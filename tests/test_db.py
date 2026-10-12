import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from watcher import db

MARKET_A, MARKET_B, CTOKEN_USDC, CTOKEN_WMON, ORACLE = ("0x" + byte * 20 for byte in ("a1", "a2", "c1", "c2", "0e"))


class DatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp()) / "program" / "watcher.db"
        self.connection = db.init(self.path)
        self.addCleanup(self.connection.close)

    def add_contract(self, address: str, name: str, code: int | None = None) -> None:
        self.connection.execute(
            "INSERT INTO contracts (address, name, in_scope, origin, code) VALUES (?, ?, 1, 'manual', ?)", (address, name, code)
        )

    def add_code(self, source: str, contract_name: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO codes (source_hash, contract_name) VALUES (?, ?)", (hashlib.sha256(source.encode()).hexdigest(), contract_name)
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def add_dependency(self, contract: str, name: str, slot: str | None, kind: str, targets: list[str]) -> int:
        cursor = self.connection.execute(
            "INSERT INTO dependencies (contract, name, slot, kind) VALUES (?, ?, ?, ?)", (contract, name, slot, kind)
        )
        assert cursor.lastrowid is not None
        for target in targets:
            self.connection.execute("INSERT INTO dependency_targets VALUES (?, ?, 'manual')", (cursor.lastrowid, target))
        return cursor.lastrowid

    def test_init_creates_the_schema_and_is_idempotent(self) -> None:
        tables = {row[0] for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"codes", "contracts", "dependencies", "dependency_targets", "dependency_events", "state"})
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.add_contract(ORACLE, "OracleManager")
        self.connection.commit()
        again = db.init(self.path)
        self.addCleanup(again.close)
        self.assertEqual(again.execute("SELECT name FROM contracts").fetchall(), [("OracleManager",)])

    def test_dependencies_link_many_to_many(self) -> None:
        for address, name in ((MARKET_A, "MarketManager"), (MARKET_B, "MarketManager"), (CTOKEN_USDC, "BorrowableCToken"),
                              (CTOKEN_WMON, "BorrowableCToken"), (ORACLE, "OracleManager")):
            self.add_contract(address, name)
        self.add_dependency(MARKET_A, "oracleManager", "0x05", "single", [ORACLE])
        tokens = self.add_dependency(MARKET_A, "cTokens", None, "list", [CTOKEN_USDC, CTOKEN_WMON])
        self.add_dependency(MARKET_B, "oracleManager", "0x05", "single", [ORACLE])
        self.connection.execute("INSERT INTO dependency_events VALUES (?, 'TokenListed(address indexed cToken)', 'cToken')", (tokens,))

        depends_on = self.connection.execute(
            "SELECT d.name, t.address FROM dependencies d JOIN dependency_targets t ON t.dependency = d.id "
            "WHERE d.contract = ? ORDER BY d.name, t.address", (MARKET_A,)).fetchall()
        self.assertEqual(depends_on, [("cTokens", CTOKEN_USDC), ("cTokens", CTOKEN_WMON), ("oracleManager", ORACLE)])
        dependents = self.connection.execute(
            "SELECT d.contract FROM dependency_targets t JOIN dependencies d ON d.id = t.dependency "
            "WHERE t.address = ? ORDER BY d.contract", (ORACLE,)).fetchall()
        self.assertEqual(dependents, [(MARKET_A,), (MARKET_B,)])

    def test_contracts_share_one_code(self) -> None:
        version_1 = self.add_code("contract BorrowableCToken { uint v = 1; }", "BorrowableCToken")
        version_2 = self.add_code("contract BorrowableCToken { uint v = 2; }", "BorrowableCToken")
        self.add_contract(CTOKEN_USDC, "BorrowableCToken (USDC)", version_1)
        self.add_contract(CTOKEN_WMON, "BorrowableCToken (WMON)", version_1)
        self.add_contract(MARKET_A, "MarketManager", version_2)
        self.add_contract(ORACLE, "Unverified OracleManager")
        per_code = self.connection.execute(
            "SELECT c.contract_name, count(k.address), min(k.address) FROM codes c JOIN contracts k ON k.code = c.id "
            "GROUP BY c.id ORDER BY c.id").fetchall()
        self.assertEqual(per_code, [("BorrowableCToken", 2, CTOKEN_USDC), ("BorrowableCToken", 1, MARKET_A)])
        self.assertEqual(self.connection.execute("SELECT address FROM contracts WHERE code IS NULL").fetchall(), [(ORACLE,)])

    def test_agent_is_a_valid_origin(self) -> None:
        self.connection.execute("INSERT INTO contracts (address, name, in_scope, origin) VALUES (?, 'Found', 0, 'agent')", (ORACLE,))
        self.assertEqual(self.connection.execute("SELECT origin FROM contracts").fetchall(), [("agent",)])

    def test_constraints_reject_invalid_data(self) -> None:
        self.add_contract(MARKET_A, "MarketManager")
        oracle = self.add_dependency(MARKET_A, "oracleManager", "0x05", "single", [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("INSERT INTO dependency_targets VALUES (?, ?, 'manual')", (oracle, ORACLE))
        self.add_contract(ORACLE, "OracleManager")
        self.connection.execute("INSERT INTO dependency_targets VALUES (?, ?, 'manual')", (oracle, ORACLE))
        for statement, parameters in (
            ("INSERT INTO dependencies (contract, name, slot, kind) VALUES (?, 'oracleManager', '0x06', 'single')", (MARKET_A,)),
            ("INSERT INTO contracts (address, name, in_scope, origin) VALUES (?, 'Upper', 1, 'manual')", (MARKET_B.upper().replace("0X", "0x"),)),
            ("INSERT INTO contracts (address, name, in_scope, origin) VALUES (?, 'Short', 1, 'manual')", ("0x1234",)),
            ("INSERT INTO contracts (address, name, in_scope, origin) VALUES (?, 'Origin', 1, 'guess')", (MARKET_B,)),
            ("INSERT INTO contracts (address, name, in_scope, origin, code) VALUES (?, 'Unknown code', 1, 'manual', 999)", (MARKET_B,)),
            ("INSERT INTO codes (source_hash, contract_name) VALUES ('abc', 'TooShort')", ()),
            ("INSERT INTO dependencies (contract, name, slot, kind) VALUES (?, 'x', '0x07', 'many')", (MARKET_A,)),
            ("INSERT INTO dependencies (contract, name, slot, kind) VALUES (?, 'y', '7', 'single')", (MARKET_A,)),
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute(statement, parameters)

    def test_deleting_a_dependency_removes_its_targets_and_events(self) -> None:
        self.add_contract(MARKET_A, "MarketManager")
        self.add_contract(CTOKEN_USDC, "BorrowableCToken")
        tokens = self.add_dependency(MARKET_A, "cTokens", None, "list", [CTOKEN_USDC])
        self.connection.execute("INSERT INTO dependency_events VALUES (?, 'TokenListed(address indexed cToken)', 'cToken')", (tokens,))
        self.connection.execute("DELETE FROM dependencies WHERE id = ?", (tokens,))
        counts = [self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("dependency_targets", "dependency_events")]
        self.assertEqual(counts, [0, 0])

    def test_prune_removes_what_no_in_scope_contract_needs(self) -> None:
        market_code, oracle_code, token_code = (self.add_code(name, name) for name in ("MarketManager", "OracleManager", "BorrowableCToken"))
        self.add_contract(MARKET_A, "Market A", market_code)
        self.add_contract(MARKET_B, "Market B", market_code)
        self.add_contract(CTOKEN_WMON, "cToken WMON", token_code)
        for address, code in ((ORACLE, oracle_code), (CTOKEN_USDC, token_code)):
            self.connection.execute("INSERT INTO contracts (address, name, in_scope, origin, code) VALUES (?, 'found', 0, 'agent', ?)", (address, code))
        self.add_dependency(MARKET_A, "oracleManager", None, "single", [ORACLE])
        self.add_dependency(MARKET_A, "cTokens", None, "list", [CTOKEN_USDC])
        self.add_dependency(MARKET_B, "oracleManager", None, "single", [ORACLE])
        self.add_dependency(MARKET_B, "cTokens", None, "list", [CTOKEN_WMON])
        self.add_dependency(CTOKEN_WMON, "marketManager", None, "single", [MARKET_B])

        self.assertEqual(db.prune(self.connection, [MARKET_B]), db.Pruned([MARKET_A, CTOKEN_WMON], [MARKET_A, CTOKEN_USDC], []))
        contracts = self.connection.execute("SELECT address, in_scope FROM contracts ORDER BY address").fetchall()
        self.assertEqual(contracts, [(ORACLE, 0), (MARKET_B, 1), (CTOKEN_WMON, 0)])
        dependencies = self.connection.execute("SELECT contract, name FROM dependencies ORDER BY name").fetchall()
        self.assertEqual(dependencies, [(MARKET_B, "cTokens"), (CTOKEN_WMON, "marketManager"), (MARKET_B, "oracleManager")])

        pruned = db.prune(self.connection, [])
        self.assertEqual(pruned.deleted, [ORACLE, MARKET_B, CTOKEN_WMON])
        self.assertEqual(sorted(name for name, _ in pruned.codes), ["BorrowableCToken", "MarketManager", "OracleManager"])
        self.assertEqual([self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("contracts", "codes", "dependencies")], [0, 0, 0])

    def test_prune_keeps_everything_reachable_from_the_scope(self) -> None:
        self.add_contract(MARKET_A, "Market A")
        for address in (ORACLE, CTOKEN_USDC, CTOKEN_WMON):
            self.connection.execute("INSERT INTO contracts (address, name, in_scope, origin) VALUES (?, 'found', 0, 'agent')", (address,))
        self.add_dependency(MARKET_A, "oracleManager", None, "single", [ORACLE])
        self.add_dependency(ORACLE, "implementation", "0x01", "single", [CTOKEN_USDC])
        self.add_dependency(CTOKEN_USDC, "back", None, "single", [ORACLE])
        self.add_dependency(CTOKEN_WMON, "orphan", None, "single", [ORACLE])

        self.assertEqual(db.prune(self.connection, [MARKET_A]).deleted, [CTOKEN_WMON])
        dependencies = self.connection.execute("SELECT contract, name FROM dependencies ORDER BY name").fetchall()
        self.assertEqual(dependencies, [(CTOKEN_USDC, "back"), (ORACLE, "implementation"), (MARKET_A, "oracleManager")])
        self.assertEqual(db.prune(self.connection, [MARKET_A]).deleted, [])
        self.assertEqual(db.prune(self.connection, []), db.Pruned([MARKET_A], [ORACLE, MARKET_A, CTOKEN_USDC], []))
        self.assertEqual(self.connection.execute("SELECT count(*) FROM dependencies").fetchone(), (0,))

    def test_unknown_schema_version_is_refused(self) -> None:
        self.connection.execute("PRAGMA user_version = 99")
        with self.assertRaises(RuntimeError):
            db.init(self.path)


if __name__ == "__main__":
    unittest.main()
