import contextlib
import io
import json
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from pydantic import ValidationError

from watcher import db
from watcher.codes import clone_codes, register, remove_codes
from watcher.prompt import dependency_prompt
from watcher.etherscan import Etherscan
from watcher.main import fill
from watcher.main import sync as main_sync
from watcher.scope import load_scope

MARKET, CTOKEN_A, CTOKEN_B, CTOKEN_RENAMED, UNVERIFIED = (
    "0x" + byte * 20 for byte in ("aa", "c1", "c2", "c3", "ee")
)
CTOKEN_SOURCE = "contract BorrowableCToken {}"


class FillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sources: dict[str, dict[str, str]] = {
            MARKET: {"SourceCode": json.dumps({"MarketManager.sol": {"content": "contract MarketManager {}"}}), "ContractName": "MarketManager", "ABI": "[]"},
            CTOKEN_A: {"SourceCode": CTOKEN_SOURCE, "ContractName": "BorrowableCToken", "ABI": "[]"},
            CTOKEN_B: {"SourceCode": CTOKEN_SOURCE, "ContractName": "BorrowableCToken", "ABI": "[]"},
            CTOKEN_RENAMED: {"SourceCode": CTOKEN_SOURCE, "ContractName": "OtherContract", "ABI": "[]"},
            UNVERIFIED: {"SourceCode": "", "ContractName": ""},
        }
        self.fail_requests = False
        self.requests: list[str] = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                test.requests.append(query["address"])
                if test.fail_requests:
                    body: dict[str, object] = {"status": "0", "message": "NOTOK", "result": "Max rate limit reached"}
                else:
                    body = {"status": "1", "message": "OK", "result": [test.sources[query["address"]]]}
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.root = Path(tempfile.mkdtemp())
        self.url = f"http://127.0.0.1:{server.server_port}/v2/api"
        self.etherscan = Etherscan("key", 143, self.url, min_interval=0, attempts=1)
        self.connection = db.init(self.root / "watcher.db")
        self.addCleanup(self.connection.close)

    def write_scope(self, contracts: list[dict[str, str]], **extra: object) -> Path:
        path = self.root / "scope.json"
        path.write_text(json.dumps({"program": "curvance", "chain": "monad", "contracts": contracts, **extra}))
        return path

    def rows(self) -> list[tuple[str, str, int, str, str | None]]:
        return self.connection.execute(
            "SELECT k.address, k.name, k.in_scope, k.origin, c.contract_name FROM contracts k LEFT JOIN codes c ON c.id = k.code ORDER BY k.address"
        ).fetchall()

    def test_fill_stores_contracts_and_shares_codes(self) -> None:
        scope = load_scope(self.write_scope([
            {"name": "Market Manager", "address": MARKET.upper().replace("0X", "0x")},
            {"name": "cToken A", "address": CTOKEN_A},
            {"name": "cToken B", "address": CTOKEN_B},
            {"name": "Renamed", "address": CTOKEN_RENAMED},
            {"name": "Unverified", "address": UNVERIFIED},
        ]))
        self.assertEqual(fill(self.connection, scope, self.etherscan), [UNVERIFIED])
        self.assertEqual(self.rows(), [
            (MARKET, "Market Manager", 1, "manual", "MarketManager"),
            (CTOKEN_A, "cToken A", 1, "manual", "BorrowableCToken"),
            (CTOKEN_B, "cToken B", 1, "manual", "BorrowableCToken"),
            (CTOKEN_RENAMED, "Renamed", 1, "manual", "OtherContract"),
            (UNVERIFIED, "Unverified", 1, "manual", None),
        ])
        self.assertEqual(self.connection.execute("SELECT count(*) FROM codes").fetchone()[0], 3)
        shared = self.connection.execute("SELECT count(DISTINCT code) FROM contracts WHERE address IN (?, ?)", (CTOKEN_A, CTOKEN_B)).fetchone()[0]
        self.assertEqual(shared, 1)

        scope = load_scope(self.write_scope([{"name": "cToken A (USDC)", "address": CTOKEN_A}, {"name": "Market Manager", "address": MARKET},
                                             {"name": "Unverified", "address": UNVERIFIED}]))
        requests = len(self.requests)
        self.assertEqual(fill(self.connection, scope, Etherscan("key", 143, self.url, min_interval=0, attempts=1)), [UNVERIFIED])
        self.assertEqual(self.requests[requests:], [UNVERIFIED])
        self.assertEqual(len(self.rows()), 5)
        self.assertEqual(self.connection.execute("SELECT name FROM contracts WHERE address = ?", (CTOKEN_A,)).fetchone()[0], "cToken A (USDC)")
        self.assertEqual(self.connection.execute("SELECT count(*) FROM codes").fetchone()[0], 3)

    def test_codes_are_cloned_once_and_the_prompt_lists_every_contract(self) -> None:
        scope = load_scope(self.write_scope([
            {"name": "Market Manager", "address": MARKET},
            {"name": "cToken A", "address": CTOKEN_A},
            {"name": "cToken B", "address": CTOKEN_B},
            {"name": "Unverified", "address": UNVERIFIED},
        ]))
        codes_dir = self.root / "codes"
        fill(self.connection, scope, self.etherscan)
        cloned = clone_codes(self.connection, self.etherscan, codes_dir)
        self.assertEqual(sorted(folder.name.split("-")[0] for folder in cloned), ["BorrowableCToken", "MarketManager"])
        self.assertEqual(len(self.requests), 4)
        files = {str(path.relative_to(codes_dir)).split("-", 1)[0] + "/" + path.name: path.read_text() for path in codes_dir.rglob("*.sol")}
        self.assertEqual(files, {"BorrowableCToken/BorrowableCToken.sol": CTOKEN_SOURCE, "MarketManager/MarketManager.sol": "contract MarketManager {}"})
        self.assertEqual(sorted(path.name for path in (self.root / "abis").iterdir()), sorted(f"{folder.name}.json" for folder in cloned))
        self.assertFalse(any((folder / "abi.json").exists() for folder in cloned))

        next_run = Etherscan("key", 143, self.url, min_interval=0, attempts=1)
        self.assertEqual(clone_codes(self.connection, next_run, codes_dir), [])
        self.assertEqual(len(self.requests), 4)
        token_folder = next(folder for folder in cloned if folder.name.startswith("BorrowableCToken"))
        shutil.rmtree(token_folder)
        self.assertEqual(clone_codes(self.connection, next_run, codes_dir), [token_folder])
        self.assertEqual(len(self.requests), 5)
        (self.root / "abis" / f"{token_folder.name}.json").unlink()
        self.assertEqual(clone_codes(self.connection, next_run, codes_dir), [])
        self.assertTrue((self.root / "abis" / f"{token_folder.name}.json").exists())

        scope = load_scope(self.write_scope([{"name": "Market Manager", "address": MARKET}, {"name": "Unverified", "address": UNVERIFIED}]))
        with self.connection:
            pruned = db.prune(self.connection, [MARKET, UNVERIFIED])
        self.assertEqual((pruned.out_of_scope, pruned.deleted, [name for name, _ in pruned.codes]), ([CTOKEN_A, CTOKEN_B], [CTOKEN_A, CTOKEN_B], ["BorrowableCToken"]))
        remove_codes(codes_dir, pruned.codes)
        self.assertEqual([path.name for path in codes_dir.iterdir()], [folder.name for folder in cloned if folder.name.startswith("MarketManager")])
        self.assertEqual([path.stem for path in (self.root / "abis").iterdir()], [folder.name for folder in cloned if folder.name.startswith("MarketManager")])
        prompt = dependency_prompt(self.connection, scope, codes_dir)
        self.assertIn("bug bounty program curvance on the chain monad (chain ID 143, Alchemy network monad-mainnet)", prompt)
        self.assertIn(f"- Unverified at {UNVERIFIED}: no verified source", prompt)
        self.assertNotIn("<", prompt)

    def test_the_agent_only_runs_when_contracts_are_new_in_scope(self) -> None:
        def sync() -> list[str]:
            with mock.patch("watcher.main.run_agent", return_value={"result": "done"}) as agent, contextlib.redirect_stdout(io.StringIO()):
                main_sync(self.connection, self.root, load_scope(self.root / "scope.json"), self.etherscan)
            return [call.args[0] for call in agent.call_args_list]

        self.write_scope([{"name": "Market Manager", "address": MARKET}])
        self.assertEqual(len(sync()), 1)
        self.assertEqual(sync(), [])
        self.write_scope([{"name": "Market Manager", "address": MARKET}, {"name": "cToken A", "address": CTOKEN_A}])
        self.assertEqual(len(sync()), 1)
        self.write_scope([{"name": "cToken A", "address": CTOKEN_A}])
        self.assertEqual(sync(), [])

    def test_dependency_codes_live_in_their_own_folder(self) -> None:
        codes_dir = self.root / "codes"
        with self.connection:
            db.save_contract(self.connection, MARKET, "Market Manager", self.etherscan.code(MARKET))
        clone_codes(self.connection, self.etherscan, codes_dir)
        register(self.connection, self.etherscan, codes_dir, CTOKEN_A, None, "agent")
        dependencies = self.root / "dependencies"
        self.assertEqual([path.name.split("-")[0] for path in codes_dir.iterdir()], ["MarketManager"])
        self.assertEqual([path.name.split("-")[0] for path in dependencies.iterdir()], ["BorrowableCToken"])
        requests = len(self.requests)

        with self.connection:
            db.save_contract(self.connection, CTOKEN_A, "cToken A", self.etherscan.code(CTOKEN_A))
        clone_codes(self.connection, self.etherscan, codes_dir)
        self.assertEqual(sorted(path.name.split("-")[0] for path in codes_dir.iterdir()), ["BorrowableCToken", "MarketManager"])
        self.assertEqual(list(dependencies.iterdir()), [])
        with self.connection:
            self.connection.execute("UPDATE contracts SET in_scope = 0 WHERE address = ?", (CTOKEN_A,))
        clone_codes(self.connection, self.etherscan, codes_dir)
        self.assertEqual([path.name.split("-")[0] for path in dependencies.iterdir()], ["BorrowableCToken"])
        self.assertEqual(len(self.requests), requests)

    def test_a_changed_source_is_not_cloned(self) -> None:
        scope = load_scope(self.write_scope([{"name": "Market Manager", "address": MARKET}]))
        fill(self.connection, scope, self.etherscan)
        self.sources[MARKET] = {"SourceCode": "contract MarketManager { uint v = 2; }", "ContractName": "MarketManager", "ABI": "[]"}
        with self.assertRaises(RuntimeError):
            clone_codes(self.connection, Etherscan("key", 143, self.url, min_interval=0, attempts=1), self.root / "codes")
        self.assertEqual(list((self.root / "codes").iterdir()) if (self.root / "codes").exists() else [], [])

    def test_a_failed_request_writes_nothing(self) -> None:
        scope = load_scope(self.write_scope([{"name": "Market Manager", "address": MARKET}]))
        self.fail_requests = True
        with self.assertRaises(RuntimeError):
            fill(self.connection, scope, self.etherscan)
        self.assertEqual(self.rows(), [])

    def test_invalid_scope_files_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            load_scope(self.write_scope([{"name": "a", "address": CTOKEN_A}, {"name": "b", "address": CTOKEN_A.upper().replace("0X", "0x")}]))
        for contracts, extra in (([], {}), ([{"name": "short", "address": "0x1234"}], {}), ([{"name": "a", "address": CTOKEN_A}], {"source": "manual"})):
            with self.subTest(contracts=contracts, extra=extra), self.assertRaises(ValidationError):
                load_scope(self.write_scope(contracts, **extra))


if __name__ == "__main__":
    unittest.main()
