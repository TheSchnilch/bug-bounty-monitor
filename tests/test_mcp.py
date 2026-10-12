import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mcp.shared.memory import create_connected_server_and_client_session

from watcher import db
from watcher.codes import clone_codes
from watcher.etherscan import Etherscan
from watcher.mcp_server import REPO_ROOT, create_server, write_mcp_config
from watcher.rpc import Rpc

MARKET, ORACLE, CTOKEN_A, CTOKEN_B, UNVERIFIED, WALLET = ("0x" + byte * 20 for byte in ("aa", "0e", "c1", "c2", "ee", "99"))
MARKET_ABI = [
    {"type": "event", "name": "TokenListed", "inputs": [{"name": "cToken", "type": "address", "indexed": True}, {"name": "id", "type": "uint256"}]},
    {"type": "event", "name": "Configured", "inputs": [{"name": "config", "type": "tuple[]", "components": [{"name": "a", "type": "address"}, {"name": "b", "type": "uint8"}]}]},
    {"type": "function", "name": "listToken", "inputs": [{"name": "cToken", "type": "address"}]},
]


def entry(name: str, abi: list[dict[str, Any]]) -> dict[str, str]:
    return {"SourceCode": f"contract {name} {{}}", "ContractName": name, "ABI": json.dumps(abi)}


class McpServerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.sources = {
            MARKET: entry("MarketManagerIsolated", MARKET_ABI),
            ORACLE: entry("OracleManager", []),
            CTOKEN_A: entry("BorrowableCToken", []),
            CTOKEN_B: entry("BorrowableCToken", []),
            UNVERIFIED: {"SourceCode": "", "ContractName": "", "ABI": "Contract source code not verified"},
        }
        self.storage = {(MARKET, "0x5"): "0x" + "0" * 24 + ORACLE[2:]}
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                self.reply({"status": "1", "message": "OK", "result": [test.sources[query["address"]]]})

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                params = request["params"]
                if request["method"] == "eth_getCode":
                    result = "0x6080" if params[0] in test.sources else "0x"
                else:
                    result = test.storage.get((params[0], params[1]), "0x" + "0" * 64)
                self.reply({"jsonrpc": "2.0", "id": request["id"], "result": result})

            def reply(self, body: dict[str, Any]) -> None:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        self.etherscan = Etherscan("key", 143, f"{url}/v2/api", min_interval=0, attempts=1)
        self.root = Path(tempfile.mkdtemp())
        connection = db.init(self.root / "watcher.db")
        with connection:
            db.save_contract(connection, MARKET, "Market Manager", self.etherscan.code(MARKET))
        clone_codes(connection, self.etherscan, self.root / "codes")
        connection.close()
        self.server = create_server(self.root, self.etherscan, Rpc(url))

    async def call(self, tool: str, **arguments: object) -> Any:
        async with create_connected_server_and_client_session(self.server) as client:
            result = await client.call_tool(tool, arguments)
        if result.isError:
            raise ValueError(" ".join(getattr(item, "text", "") for item in result.content))
        assert result.structuredContent is not None
        return result.structuredContent.get("result", result.structuredContent)

    async def test_list_contracts_returns_the_stored_contracts(self) -> None:
        self.assertEqual(await self.call("list_contracts"), [
            {"address": MARKET, "name": "Market Manager", "in_scope": True, "origin": "manual",
             "code_folder": str(next((self.root / "codes").iterdir()))},
        ])

    async def test_add_contract_registers_and_clones_agent_contracts(self) -> None:
        oracle = await self.call("add_contract", address=ORACLE.upper().replace("0X", "0x"), name="Oracle Manager")
        self.assertEqual({key: oracle[key] for key in ("address", "in_scope", "origin")}, {"address": ORACLE, "in_scope": False, "origin": "agent"})
        self.assertEqual(json.loads((self.root / "abis" / f"{Path(oracle['code_folder']).name}.json").read_text()), [])
        self.assertEqual(await self.call("add_contract", address=ORACLE, name="Renamed"), oracle)
        unverified = await self.call("add_contract", address=UNVERIFIED, name="Unverified")
        self.assertIsNone(unverified["code_folder"])
        await self.call("add_contract", address=CTOKEN_A, name="cToken A")
        await self.call("add_contract", address=CTOKEN_B, name="cToken B")
        codes = await self.call("list_codes")
        self.assertEqual([(code["contract_name"], code["addresses"]) for code in codes], [
            ("BorrowableCToken", [CTOKEN_A, CTOKEN_B]), ("MarketManagerIsolated", [MARKET]), ("OracleManager", [ORACLE]),
        ])
        with self.assertRaisesRegex(ValueError, "no contract is deployed"):
            await self.call("add_contract", address=WALLET, name="Wallet")

    async def test_dependencies_need_registered_targets_and_a_matching_slot(self) -> None:
        with self.assertRaisesRegex(ValueError, "call add_contract first"):
            await self.call("add_dependency", contract=MARKET, name="oracleManager", kind="single", targets=[ORACLE], slot="0x05")
        await self.call("add_contract", address=ORACLE, name="Oracle Manager")
        await self.call("add_contract", address=CTOKEN_A, name="cToken A")
        with self.assertRaisesRegex(ValueError, f"holds 0x{'0' * 40}"):
            await self.call("add_dependency", contract=MARKET, name="oracleManager", kind="single", targets=[ORACLE], slot="0x06")
        with self.assertRaisesRegex(ValueError, "exactly one target"):
            await self.call("add_dependency", contract=MARKET, name="oracleManager", kind="single", targets=[ORACLE, CTOKEN_A])
        await self.call("add_dependency", contract=MARKET, name="oracleManager", kind="single", targets=[ORACLE], slot="0x05")
        with self.assertRaisesRegex(ValueError, "already has a dependency"):
            await self.call("add_dependency", contract=MARKET, name="oracleManager", kind="single", targets=[ORACLE])
        with self.assertRaisesRegex(ValueError, "single dependency"):
            await self.call("add_dependency_target", contract=MARKET, dependency="oracleManager", address=CTOKEN_A)

        await self.call("add_dependency", contract=MARKET, name="cTokens", kind="list", targets=[])
        await self.call("add_dependency_target", contract=MARKET, dependency="cTokens", address=CTOKEN_A)
        market = await self.call("get_contract", address=MARKET)
        self.assertEqual(market["dependencies"], [
            {"name": "cTokens", "slot": None, "kind": "list", "targets": [CTOKEN_A], "events": []},
            {"name": "oracleManager", "slot": "0x5", "kind": "single", "targets": [ORACLE], "events": []},
        ])
        market = await self.call("remove_dependency", contract=MARKET, dependency="oracleManager")
        self.assertEqual([dependency["name"] for dependency in market["dependencies"]], ["cTokens"])

    async def test_events_are_checked_against_the_abi(self) -> None:
        await self.call("add_dependency", contract=MARKET, name="cTokens", kind="list", targets=[])
        with self.assertRaisesRegex(ValueError, r"its events are: Configured\(\(address,uint8\)\[\]\), TokenListed\(address,uint256\)"):
            await self.call("add_dependency_event", contract=MARKET, dependency="cTokens", signature="TokenListed(address indexed cToken)", field="cToken")
        with self.assertRaisesRegex(ValueError, "its address parameters are: cToken"):
            await self.call("add_dependency_event", contract=MARKET, dependency="cTokens", signature="TokenListed(address,uint256)", field="id")
        market = await self.call("add_dependency_event", contract=MARKET, dependency="cTokens", signature="TokenListed(address,uint256)", field="cToken")
        self.assertEqual(market["dependencies"][0]["events"], [{"signature": "TokenListed(address,uint256)", "field": "cToken"}])

        await self.call("add_contract", address=UNVERIFIED, name="Unverified")
        await self.call("add_dependency", contract=UNVERIFIED, name="tokens", kind="list", targets=[])
        with self.assertRaisesRegex(ValueError, "no verified source"):
            await self.call("add_dependency_event", contract=UNVERIFIED, dependency="tokens", signature="A(address)", field="a")


class McpConfigTest(unittest.TestCase):
    def test_config_starts_the_server_module_for_the_data_root(self) -> None:
        root = Path(tempfile.mkdtemp())
        config = json.loads(write_mcp_config(root).read_text())
        self.assertEqual(config, {"mcpServers": {"watcher": {
            "command": sys.executable,
            "args": ["-m", "watcher.mcp_server", "--data-root", str(root)],
            "env": {"PYTHONPATH": str(REPO_ROOT)},
        }}})


if __name__ == "__main__":
    unittest.main()
