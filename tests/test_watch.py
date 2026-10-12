import json
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from watcher import db
from watcher.codes import clone_codes, register
from watcher.etherscan import Etherscan
from watcher.rpc import Rpc
from watcher.watch import Watch, topic

MARKET, ORACLE, ORACLE_V2, CTOKEN, MANAGER, WALLET = ("0x" + byte * 20 for byte in ("aa", "0e", "0f", "c1", "9a", "99"))
TOKEN_LISTED, MANAGER_UPDATED = "TokenListed(address)", "PositionManagerUpdated(address,bool)"
MARKETS_ADDED, RISK_MANAGER_SET = "MarketsAdded(address[])", "RiskManagerSet(address)"
MARKET_ABI = [
    {"type": "event", "name": "TokenListed", "inputs": [{"name": "cToken", "type": "address", "indexed": True}]},
    {"type": "event", "name": "PositionManagerUpdated", "inputs": [{"name": "positionManager", "type": "address"}, {"name": "add", "type": "bool"}]},
    {"type": "event", "name": "MarketsAdded", "inputs": [{"name": "markets", "type": "address[]"}]},
    {"type": "event", "name": "RiskManagerSet", "inputs": [{"name": "", "type": "address", "indexed": True}]},
]
TOKEN_SCRIPT = 'def handle(event):\n    return [event["args"]["cToken"]], []\n'
MANAGER_SCRIPT = """def handle(event):
    manager = event["args"]["positionManager"]
    return ([manager], []) if event["args"]["add"] else ([], [manager])
"""
MARKETS_SCRIPT = 'def handle(event):\n    return event["args"]["markets"], []\n'
RISK_SCRIPT = 'def handle(event):\n    return [event["args"]["arg0"]], []\n'
BROKEN_SCRIPT = 'def handle(event):\n    raise ValueError("boom")\n'


def entry(name: str, body: str = "", abi: list[dict[str, Any]] | None = None) -> dict[str, str]:
    return {"SourceCode": f"contract {name} {{{body}}}", "ContractName": name, "ABI": json.dumps(abi or [])}


def word(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


class WatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = {address: entry(name) for address, name in ((ORACLE, "OracleManager"), (ORACLE_V2, "OracleManagerV2"),
                                                                    (CTOKEN, "BorrowableCToken"), (MANAGER, "PositionManager"))}
        self.sources[MARKET] = entry("MarketManager", abi=MARKET_ABI)
        self.code = {address: "0x60" + address[2:6] for address in self.sources}
        self.storage = {(MARKET, "0x5"): word(ORACLE)}
        self.logs: list[dict[str, Any]] = []
        self.head = 100
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
                if query["module"] == "logs":
                    logs = test.find_logs(query["address"], query["topic0"], int(query["fromBlock"]), int(query["toBlock"]))
                    self.reply({"status": "1", "message": "OK", "result": logs} if logs else {"status": "0", "message": "No records found", "result": []})
                else:
                    self.reply({"status": "1", "message": "OK", "result": [test.sources[query["address"]]]})

            def do_POST(self) -> None:
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.reply({"jsonrpc": "2.0", "id": request["id"], "result": test.rpc(request["method"], request["params"])})

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
        self.rpc_client = Rpc(url)
        self.root = Path(tempfile.mkdtemp())
        self.connection = db.init(self.root / "watcher.db")
        self.addCleanup(self.connection.close)
        with self.connection:
            db.save_contract(self.connection, MARKET, "Market Manager", self.etherscan.code(MARKET))
        clone_codes(self.connection, self.etherscan, self.root / "codes")
        register(self.connection, self.etherscan, self.root / "codes", ORACLE, "Oracle Manager", "agent")
        with self.connection:
            self.dependency("oracleManager", "0x5", "single", [ORACLE])
            self.dependency("cTokens", None, "list", [], (TOKEN_LISTED, TOKEN_SCRIPT))
            self.dependency("positionManagers", None, "list", [], (MANAGER_UPDATED, MANAGER_SCRIPT))
            self.dependency("markets", None, "list", [], (MARKETS_ADDED, MARKETS_SCRIPT))
            self.dependency("riskManager", None, "single", [ORACLE], (RISK_MANAGER_SET, RISK_SCRIPT))
            self.dependency("broken", None, "list", [], (TOKEN_LISTED, BROKEN_SCRIPT))

    def dependency(self, name: str, slot: str | None, kind: str, targets: list[str], event: tuple[str, str] | None = None) -> None:
        id_ = self.connection.execute("INSERT INTO dependencies (contract, name, slot, kind) VALUES (?, ?, ?, ?)", (MARKET, name, slot, kind)).lastrowid
        self.connection.executemany("INSERT INTO dependency_targets VALUES (?, ?, 'agent')", [(id_, target) for target in targets])
        if event:
            self.connection.execute("INSERT INTO dependency_events VALUES (?, ?, ?)", (id_, *event))

    def rpc(self, method: str, params: list[Any]) -> Any:
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getCode":
            return self.code.get(params[0], "0x")
        return self.storage.get((params[0], params[1]), word("0x" + "0" * 40))

    def find_logs(self, address: str, topic0: str, start: int, end: int) -> list[dict[str, Any]]:
        return [log for log in self.logs if log["address"] == address and log["topics"][0] == topic0 and start <= int(log["blockNumber"], 16) <= end]

    def log(self, block: int, signature: str, topics: list[str], data: str = "0x") -> None:
        self.logs.append({"address": MARKET, "blockNumber": hex(block), "logIndex": "0x0", "topics": [topic(signature), *topics], "data": data})

    def watch(self) -> tuple[int, list[str]]:
        return Watch(self.connection, self.rpc_client, self.etherscan, self.root / "codes").run()

    def targets(self, name: str) -> list[tuple[str, str]]:
        return self.connection.execute(
            "SELECT t.address, t.source FROM dependency_targets t JOIN dependencies d ON d.id = t.dependency WHERE d.name = ? ORDER BY t.address", (name,)
        ).fetchall()

    def test_the_first_run_only_records_the_start(self) -> None:
        self.log(50, TOKEN_LISTED, [word(CTOKEN)])
        self.assertEqual(self.watch(), (88, []))
        self.assertEqual(self.connection.execute("SELECT block FROM state").fetchone(), (88,))
        self.assertEqual(self.targets("cTokens"), [])
        self.assertEqual(self.watch(), (88, []))

    def test_slot_and_event_changes_are_found(self) -> None:
        self.watch()
        self.storage[(MARKET, "0x5")] = word(ORACLE_V2)
        self.log(90, MANAGER_UPDATED, [], "0x" + word(MANAGER)[2:] + "0" * 63 + "1")
        self.log(92, MANAGER_UPDATED, [], "0x" + word(MANAGER)[2:] + "0" * 64)
        self.log(93, MARKETS_ADDED, [], "0x" + "0" * 62 + "20" + "0" * 63 + "2" + word(CTOKEN)[2:] + word(MANAGER)[2:])
        self.log(95, TOKEN_LISTED, [word(CTOKEN)])
        self.log(96, TOKEN_LISTED, [word(WALLET)])
        self.log(97, RISK_MANAGER_SET, [word(ORACLE_V2)])
        self.log(99, TOKEN_LISTED, [word(ORACLE)])
        self.head = 110

        block, changes = self.watch()
        self.assertEqual(block, 98)
        broken = f"{MARKET} broken: script for {TOKEN_LISTED} failed in block"
        self.assertEqual(changes, [
            f"new contract OracleManagerV2 at {ORACLE_V2}",
            f"{MARKET} oracleManager: {ORACLE} -> {ORACLE_V2}",
            f"new contract PositionManager at {MANAGER}",
            f"{MARKET} positionManagers: added {MANAGER} in block 90",
            f"{MARKET} positionManagers: removed {MANAGER} in block 92",
            f"new contract BorrowableCToken at {CTOKEN}",
            f"{MARKET} markets: added {CTOKEN} in block 93",
            f"{MARKET} markets: added {MANAGER} in block 93",
            f"{MARKET} cTokens: added {CTOKEN} in block 95",
            f"{broken} 95: ValueError: boom",
            f"{MARKET} cTokens: event added {WALLET}, which is not a contract",
            f"{broken} 96: ValueError: boom",
            f"{MARKET} riskManager: set to {ORACLE_V2} in block 97",
        ])
        self.assertEqual(self.targets("oracleManager"), [(ORACLE_V2, "slot")])
        self.assertEqual(self.targets("cTokens"), [(CTOKEN, "event")])
        self.assertEqual(self.targets("positionManagers"), [])
        self.assertEqual(self.targets("markets"), [(MANAGER, "event"), (CTOKEN, "event")])
        self.assertEqual(self.targets("riskManager"), [(ORACLE_V2, "event")])
        origins = dict(self.connection.execute("SELECT address, origin FROM contracts").fetchall())
        self.assertEqual((origins[ORACLE_V2], origins[CTOKEN], origins[MANAGER]), ("slot", "event", "event"))

        self.head = 112
        self.assertEqual(self.watch(), (100, [f"{MARKET} cTokens: added {ORACLE} in block 99", f"{broken} 99: ValueError: boom"]))


if __name__ == "__main__":
    unittest.main()
