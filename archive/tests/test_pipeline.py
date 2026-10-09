import json
import os
import subprocess
import tempfile
import textwrap
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from watcher.main import Config, check, rpc_url, setup, sync_scope
from watcher.etherscan import Etherscan, clone
from watcher.models import Scope, State
from watcher.sources import Rpc

PROXY, IMPL_V1, IMPL_SAME, IMPL_V2, PLAIN, NEW = ("0x" + byte * 20 for byte in ("a1", "b1", "b9", "b2", "c1", "d1"))
SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
AUDIT_PROMPT = (Path(__file__).resolve().parent.parent / "AUDIT_PROMPT.md").read_text()

FAKE_CLAUDE = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, pathlib, re, sys
    text, cwd = sys.stdin.read(), pathlib.Path.cwd()
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as handle:
        handle.write(json.dumps({"args": sys.argv[1:], "stdin": text, "secret": os.environ.get("WATCHER_ALCHEMY_API_KEY")}) + "\\n")
    args = sys.argv[1:]
    sessions = pathlib.Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fake"
    sessions.mkdir(parents=True, exist_ok=True)
    if "--resume" in args and not (sessions / (args[args.index("--resume") + 1] + ".jsonl")).exists():
        sys.exit("No conversation found with session ID")
    if "--session-id" in args:
        (sessions / (args[args.index("--session-id") + 1] + ".jsonl")).write_text(text)
    else:
        text = (sessions / (args[args.index("--resume") + 1] + ".jsonl")).read_text()
    if os.environ.get("FAKE_CLAUDE_FAIL"):
        sys.exit("No conversation found with session ID")
    limit = pathlib.Path(os.environ.get("FAKE_CLAUDE_LIMIT_ONCE", "/nonexistent/marker"))
    if limit.parent.exists() and not limit.exists():
        limit.write_text("hit")
        print("Claude usage limit reached")
        sys.exit(1)
    if "You are setting up a scope file" in text:
        path = pathlib.Path(re.search(r"(/\\S+scope\\.json)", text).group(1))
        scope = json.loads(path.read_text())
        for asset in scope["assets"]:
            if asset["address"] in os.environ.get("FAKE_PROXIES", "").split(","):
                asset["updateSlot"] = os.environ["FAKE_SLOT"]
        path.write_text(json.dumps(scope))
    else:
        (cwd / "audit" / "findings" / "F1.md").write_text("# finding\\n")
        (cwd / "test").mkdir(exist_ok=True)
        (cwd / "test" / "Poc.t.sol").write_text("// poc\\n")
    print(json.dumps({"type": "result", "is_error": False, "result": "done", "total_cost_usd": 0}))
""")


def word(address: str) -> str:
    return "0x" + address[2:].rjust(64, "0")


class FakeChain:
    def __init__(self) -> None:
        self.block, self.fail = 1000, False
        self.code = {PROXY: "0x6001", IMPL_V1: "0x6002", IMPL_SAME: "0x6002", IMPL_V2: "0x6003", PLAIN: "0x6004"}
        self.storage = {(PROXY, SLOT): word(IMPL_V1)}
        self.sources = {PROXY: {"SourceCode": "contract Proxy {}", "ContractName": "Proxy"},
                        PLAIN: {"SourceCode": "contract Token {}", "ContractName": "Token"}}
        self.etherscan_requests: list[dict[str, str]] = []

    def answer(self, request: dict[str, list[str] | str]) -> dict[str, object]:
        if self.fail:
            return {"jsonrpc": "2.0", "id": 1, "error": {"code": 429, "message": "rate limited"}}
        method, params = request["method"], request["params"]
        results = {
            "eth_blockNumber": lambda: hex(self.block),
            "eth_getCode": lambda: self.code.get(params[0], "0x"),
            "eth_getStorageAt": lambda: self.storage.get((params[0], params[1]), "0x" + "0" * 64),
            "eth_call": lambda: "0x",
        }
        return {"jsonrpc": "2.0", "id": 1, "result": results[str(method)]()}


def serve(chain: FakeChain) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps(chain.answer(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            chain.etherscan_requests.append(query)
            source = chain.sources.get(query["address"], {"SourceCode": "", "ContractName": ""})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": "1", "result": [source]}).encode())

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class WatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.chain = FakeChain()
        server = serve(self.chain)
        self.addCleanup(server.shutdown)
        (self.tmp / "claude").write_text(FAKE_CLAUDE)
        (self.tmp / "claude").chmod(0o755)
        self.claude_log = self.tmp / "claude-calls.jsonl"
        env = {"FAKE_CLAUDE_LOG": str(self.claude_log), "WATCHER_ALCHEMY_API_KEY": "secret", "FAKE_PROXIES": PROXY, "FAKE_SLOT": SLOT,
               "CLAUDE_CONFIG_DIR": str(self.tmp / "claude-home")}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bounties = self.tmp / "bounties.json"
        self.set_program([PROXY, PLAIN])
        self.cfg = Config(self.tmp / "data", "demo", str(self.bounties), f"http://127.0.0.1:{server.server_port}", str(self.tmp / "claude"),
                          etherscan_key="key", etherscan_url=f"http://127.0.0.1:{server.server_port}/v2/api")
        self.rpc = Rpc(self.cfg.rpc_url, attempts=1)

    def set_program(self, addresses: list[str]) -> None:
        assets = [{"type": "smart_contract", "url": f"https://etherscan.io/address/{address}", "description": f"Asset {address[:6]}"}
                  for address in addresses]
        assets.append({"type": "smart_contract", "url": "https://docs.example.com/addresses", "description": ""})
        self.bounties.write_text(json.dumps([{"slug": "demo", "assets": assets}]))

    def scope(self) -> dict[str, str | None]:
        return {asset.address: asset.updateSlot for asset in Scope.model_validate_json(self.cfg.scope_path.read_text()).assets}

    def calls(self) -> list[dict[str, str]]:
        return [json.loads(line) for line in self.claude_log.read_text().splitlines()] if self.claude_log.exists() else []

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.cfg.project, capture_output=True, text=True, check=True).stdout

    def test_scope_prompt_runs_for_new_addresses_and_keeps_slots(self) -> None:
        sync_scope(self.cfg)
        (prompt,) = self.calls()
        self.assertIn(f"The scope is already in {self.cfg.scope_path.resolve()}.", prompt["stdin"])
        self.assertEqual(self.scope(), {PROXY: SLOT, PLAIN: None})

        sync_scope(self.cfg)
        self.assertEqual(len(self.calls()), 1)

        self.set_program([PLAIN, PROXY, NEW])
        with mock.patch.dict(os.environ, {"FAKE_PROXIES": ""}):
            sync_scope(self.cfg)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.scope(), {PLAIN: None, PROXY: SLOT, NEW: None})

        self.set_program([PROXY, NEW])
        sync_scope(self.cfg)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.scope(), {PROXY: SLOT, NEW: None})

    def test_manual_monad_scope_skips_immunefi_and_clones_from_monad(self) -> None:
        self.cfg.immunefi = str(self.tmp / "no-such-bounties.json")
        self.cfg.data_root.mkdir(parents=True)
        self.cfg.scope_path.write_text(json.dumps({"program": "curvance", "source": "manual", "chain": "monad", "assets": [
            {"name": "Market Manager", "address": PROXY, "updateSlot": SLOT},
            {"name": "Token", "address": PLAIN, "updateSlot": None},
        ]}))
        self.assertEqual(check(self.cfg, self.rpc), [PROXY, PLAIN])
        (audit,) = self.calls()
        self.assertEqual(audit["stdin"], AUDIT_PROMPT)
        self.assertEqual({(r["chainid"], r["address"]) for r in self.chain.etherscan_requests}, {("143", PROXY), ("143", PLAIN)})
        self.assertEqual(sync_scope(self.cfg, force=True).source, "manual")
        self.assertEqual(len(self.calls()), 1)

    def test_rpc_url_follows_the_scope_chain(self) -> None:
        self.cfg.data_root.mkdir(parents=True)
        self.assertEqual(rpc_url({"WATCHER_ALCHEMY_API_KEY": "k"}, self.cfg.scope_path), "https://eth-mainnet.g.alchemy.com/v2/k")
        self.cfg.scope_path.write_text(json.dumps({"program": "curvance", "source": "manual", "chain": "monad", "assets": []}))
        self.assertEqual(rpc_url({"WATCHER_ALCHEMY_API_KEY": "k"}, self.cfg.scope_path), "https://monad-mainnet.g.alchemy.com/v2/k")
        self.assertEqual(rpc_url({"WATCHER_ALCHEMY_API_KEY": "k", "WATCHER_RPC_URL": "http://own"}, self.cfg.scope_path), "http://own")

    def test_usage_limit_resumes_the_same_session(self) -> None:
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_LIMIT_ONCE": str(self.tmp / "limit-hit")}), \
                mock.patch("watcher.main.time.sleep") as sleep:
            sync_scope(self.cfg)
        first, resumed = self.calls()
        session = first["args"][first["args"].index("--session-id") + 1]
        self.assertEqual(resumed["args"][resumed["args"].index("--resume") + 1], session)
        self.assertIn("Continue the task", resumed["stdin"])
        self.assertNotIn("You are setting up a scope file", resumed["stdin"])
        sleep.assert_called_once_with(60)
        self.assertEqual(self.scope(), {PROXY: SLOT, PLAIN: None})

    def test_failed_scope_prompt_restores_the_scope_file(self) -> None:
        sync_scope(self.cfg)
        before = self.cfg.scope_path.read_text()
        self.set_program([PROXY, PLAIN, NEW])
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_FAIL": "1"}), self.assertRaises(RuntimeError):
            sync_scope(self.cfg)
        self.assertEqual(self.cfg.scope_path.read_text(), before)
        sync_scope(self.cfg)
        self.assertIn(NEW, self.scope())

    def test_setup_audit_then_update_only_when_code_differs(self) -> None:
        self.assertEqual(check(self.cfg, self.rpc), [PROXY, PLAIN])
        _, audit = self.calls()
        self.assertEqual((self.cfg.project / f"Proxy-{PROXY}" / "Proxy.sol").read_text(), "contract Proxy {}")
        self.assertEqual((self.cfg.project / f"Token-{PLAIN}" / "Token.sol").read_text(), "contract Token {}")
        self.assertEqual({r["apikey"] for r in self.chain.etherscan_requests}, {"key"})
        self.assertEqual(audit["args"][:3], ["-p", "--model", "claude-opus-5-5"])
        self.assertIsNone(audit["secret"])
        self.assertEqual(audit["stdin"], AUDIT_PROMPT)
        self.assertEqual(len(list(self.cfg.findings.glob("*/F1.md"))), 1)
        self.assertFalse((self.cfg.project / "test").exists())
        self.assertFalse((self.cfg.project / "audit").exists())
        audit_branch = self.git("branch", "--list", "audit-*").strip()
        self.assertIn("test/Poc.t.sol", self.git("show", "--name-only", "--format=", audit_branch))
        self.assertEqual(self.git("rev-parse", "audited"), self.git("rev-parse", "main"))

        self.chain.storage[(PROXY, SLOT)] = word(IMPL_SAME)
        self.assertEqual(check(self.cfg, self.rpc), [])
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(State.model_validate_json(self.cfg.state_path.read_text()).fingerprints[PROXY].implementation, IMPL_SAME)

        self.chain.storage[(PROXY, SLOT)] = word(IMPL_V2)
        self.chain.sources[PROXY] = {"SourceCode": "contract Proxy { uint256 version = 2; }", "ContractName": "Proxy"}
        self.assertEqual(check(self.cfg, self.rpc), [PROXY])
        audit = self.calls()[-1]
        self.assertTrue(audit["stdin"].startswith(AUDIT_PROMPT + "\nThe contracts were just updated."))
        self.assertIn(f"+++ b/Proxy-{PROXY}/Proxy.sol", audit["stdin"])
        self.assertIn("+contract Proxy { uint256 version = 2; }", audit["stdin"])
        self.assertNotIn("Poc.t.sol", audit["stdin"])
        self.assertEqual(len(self.git("branch", "--list", "audit-*").split()), 2)

    def test_setup_clones_with_etherscan_and_run_audits_once(self) -> None:
        del self.chain.sources[PLAIN]
        self.assertEqual(setup(self.cfg, self.rpc), ([PROXY], [PLAIN]))
        self.assertEqual((self.cfg.project / f"Proxy-{PROXY}" / "Proxy.sol").read_text(), "contract Proxy {}")
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.git("log", "--format=%s").count("update"), 1)
        self.assertEqual(set(State.model_validate_json(self.cfg.state_path.read_text()).fingerprints), {PROXY})
        self.assertFalse(self.cfg.findings.exists())

        self.chain.sources[PLAIN] = {"SourceCode": "contract Token {}", "ContractName": "Token"}
        self.assertEqual(check(self.cfg, self.rpc), [PLAIN])
        self.assertTrue((self.cfg.project / f"Token-{PLAIN}" / "Token.sol").exists())
        audit = self.calls()[-1]
        self.assertEqual((len(self.calls()), audit["stdin"]), (2, AUDIT_PROMPT))
        self.assertEqual(check(self.cfg, self.rpc), [])
        self.assertEqual(len(self.calls()), 2)

    def test_failures_save_nothing_and_retry(self) -> None:
        self.chain.fail = True
        with self.assertRaises(RuntimeError):
            check(self.cfg, self.rpc)
        self.assertFalse(self.cfg.state_path.exists())
        self.chain.fail = False
        with mock.patch.dict(os.environ, {"FAKE_CLAUDE_FAIL": "1"}), self.assertRaises(RuntimeError):
            check(self.cfg, self.rpc)
        self.assertFalse(self.cfg.state_path.exists())
        self.assertEqual(check(self.cfg, self.rpc), [PROXY, PLAIN])
        self.assertTrue(self.cfg.state_path.exists())


class CloneTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sources: dict[str, dict[str, str]] = {}
        self.requests: list[dict[str, str]] = []
        sources, requests = self.sources, self.requests

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                requests.append(query)
                body = json.dumps({"status": "1", "result": [sources.get(query["address"], {"SourceCode": ""})]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.project = Path(tempfile.mkdtemp()) / "project"
        self.etherscan = Etherscan("key", 143, f"http://127.0.0.1:{server.server_port}/v2/api", min_interval=0, attempts=1)

    def tree(self) -> dict[str, str]:
        return {str(path.relative_to(self.project)): path.read_text() for path in sorted(self.project.rglob("*")) if path.is_file()}

    def test_clone_writes_every_source_format(self) -> None:
        unverified = "0x" + "e1" * 20
        standard_json = {"language": "Solidity", "sources": {
            "src/Proxy.sol": {"content": "contract Proxy {}"}, "../../escape.sol": {"content": "contract Escape {}"}}}
        self.sources.update({
            PROXY: {"SourceCode": "{" + json.dumps(standard_json) + "}", "ContractName": "Proxy"},
            IMPL_V1: {"SourceCode": json.dumps({"Vault.sol": {"content": "contract Vault {}"}}), "ContractName": "Vault"},
            PLAIN: {"SourceCode": "contract Token {}", "ContractName": "Token"},
        })
        cloned, missing = clone(self.etherscan, [PROXY, IMPL_V1, PLAIN, unverified, PLAIN.upper().replace("0X", "0x")], self.project)
        self.assertEqual((cloned, missing), ([PROXY, IMPL_V1, PLAIN], [unverified]))
        self.assertEqual(self.tree(), {
            f"Proxy-{PROXY}/src/Proxy.sol": "contract Proxy {}",
            f"Proxy-{PROXY}/escape.sol": "contract Escape {}",
            f"Vault-{IMPL_V1}/Vault.sol": "contract Vault {}",
            f"Token-{PLAIN}/Token.sol": "contract Token {}",
        })
        self.assertEqual({(r["chainid"], r["apikey"]) for r in self.requests}, {("143", "key")})

    def test_identical_contracts_share_one_folder_with_an_address_list(self) -> None:
        a, b, c, d, e = ("0x" + byte * 20 for byte in ("a1", "a2", "a3", "a4", "a5"))
        self.sources.update({
            a: {"SourceCode": "contract CToken { uint v = 1; }", "ContractName": "CToken"},
            b: {"SourceCode": "contract CToken { uint v = 1; }", "ContractName": "CToken"},
            c: {"SourceCode": "contract CToken { uint v = 2; }", "ContractName": "CToken"},
            d: {"SourceCode": "contract Oracle {}", "ContractName": "Oracle"},
            e: {"SourceCode": "contract Oracle {}", "ContractName": "Oracle"},
        })
        clone(self.etherscan, [b, a, c, d, e], self.project)
        expected = {
            f"CToken-{a}/CToken.sol": "contract CToken { uint v = 1; }",
            f"CToken-{c}/CToken.sol": "contract CToken { uint v = 2; }",
            "Oracle/Oracle.sol": "contract Oracle {}",
            f"addresses/CToken-{a}.txt": f"{a}\n{b}\n",
            "addresses/Oracle.txt": f"{d}\n{e}\n",
        }
        self.assertEqual(self.tree(), expected)

        clone(self.etherscan, [], self.project)
        self.assertEqual(self.tree(), expected)

        self.sources[e] = {"SourceCode": "contract Oracle { uint v = 2; }", "ContractName": "Oracle"}
        self.assertEqual(clone(self.etherscan, [e], self.project), ([e], []))
        self.assertEqual(self.tree(), {
            f"CToken-{a}/CToken.sol": "contract CToken { uint v = 1; }",
            f"CToken-{c}/CToken.sol": "contract CToken { uint v = 2; }",
            f"Oracle-{d}/Oracle.sol": "contract Oracle {}",
            f"Oracle-{e}/Oracle.sol": "contract Oracle { uint v = 2; }",
            f"addresses/CToken-{a}.txt": f"{a}\n{b}\n",
        })


if __name__ == "__main__":
    unittest.main()
