import sqlite3
from collections import defaultdict
from pathlib import Path

from Crypto.Hash import keccak

from .codes import abi_events, abi_file, register
from .etherscan import Etherscan
from .rpc import Rpc

CONFIRMATIONS = 12


def topic(signature: str) -> str:
    return "0x" + keccak.new(digest_bits=256, data=signature.encode()).hexdigest()


def word_address(word: str) -> str:
    return "0x" + word[-40:].lower()


class Watch:
    def __init__(self, connection: sqlite3.Connection, rpc: Rpc, etherscan: Etherscan, codes_dir: Path) -> None:
        self.connection, self.rpc, self.etherscan, self.codes_dir = connection, rpc, etherscan, codes_dir
        self.changes: list[str] = []

    def run(self) -> tuple[int, list[str]]:
        end = int(self.rpc.call("eth_blockNumber"), 16) - CONFIRMATIONS
        row = self.connection.execute("SELECT block FROM state").fetchone()
        if row is not None and end <= row[0]:
            return row[0], []
        self.slots(hex(end))
        if row is not None:
            self.events(row[0] + 1, end)
        with self.connection:
            self.connection.execute("INSERT INTO state VALUES (1, ?) ON CONFLICT (id) DO UPDATE SET block = excluded.block", (end,))
        return end, self.changes

    def ensure(self, address: str, origin: str, tag: str) -> bool:
        if self.connection.execute("SELECT 1 FROM contracts WHERE address = ?", (address,)).fetchone():
            return True
        if self.rpc.call("eth_getCode", address, tag) in ("0x", "0x0", None):
            return False
        name = register(self.connection, self.etherscan, self.codes_dir, address, None, origin)
        self.changes.append(f"new contract {name} at {address}")
        return True

    def slots(self, tag: str) -> None:
        rows = self.connection.execute(
            "SELECT d.id, d.contract, d.name, d.slot, t.address FROM dependencies d "
            "LEFT JOIN dependency_targets t ON t.dependency = d.id WHERE d.slot IS NOT NULL ORDER BY d.id"
        ).fetchall()
        for id_, contract, name, slot, current in rows:
            address = word_address(self.rpc.call("eth_getStorageAt", contract, slot, tag))
            if address == current:
                continue
            if not self.ensure(address, "slot", tag):
                self.changes.append(f"{contract} {name}: slot {slot} now holds {address}, which is not a contract")
                continue
            with self.connection:
                self.connection.execute("DELETE FROM dependency_targets WHERE dependency = ?", (id_,))
                self.connection.execute("INSERT INTO dependency_targets VALUES (?, ?, 'slot')", (id_, address))
            self.changes.append(f"{contract} {name}: {current} -> {address}")

    def events(self, start: int, end: int) -> None:
        watched: dict[str, dict[str, list[tuple[int, str, str, int]]]] = defaultdict(lambda: defaultdict(list))
        rows = self.connection.execute(
            "SELECT d.id, d.contract, d.name, e.signature, e.field, c.contract_name, c.source_hash FROM dependency_events e "
            "JOIN dependencies d ON d.id = e.dependency JOIN contracts k ON k.address = d.contract JOIN codes c ON c.id = k.code"
        ).fetchall()
        for id_, contract, name, signature, field, contract_name, source_hash in rows:
            inputs = abi_events(abi_file(self.codes_dir, contract_name, source_hash)).get(signature)
            if inputs is None:
                continue
            indexed = [i["name"] for i in inputs if i.get("indexed")]
            if field in indexed:
                watched[contract][topic(signature)].append((id_, name, "topic", 1 + indexed.index(field)))
            else:
                watched[contract][topic(signature)].append((id_, name, "data", [i["name"] for i in inputs if not i.get("indexed")].index(field)))
        for contract, topics in watched.items():
            logs = [log for topic0 in sorted(topics) for log in self.etherscan.logs(contract, topic0, start, end)]
            for log in sorted(logs, key=lambda log: (int(log["blockNumber"], 16), int(log["logIndex"] or "0x0", 16))):
                for id_, name, where, index in topics[log["topics"][0]]:
                    word = log["topics"][index] if where == "topic" else log["data"][2 + 64 * index:2 + 64 * (index + 1)]
                    address = word_address(word)
                    if not self.ensure(address, "event", hex(end)):
                        self.changes.append(f"{contract} {name}: event added {address}, which is not a contract")
                        continue
                    with self.connection:
                        added = self.connection.execute("INSERT OR IGNORE INTO dependency_targets VALUES (?, ?, 'event')", (id_, address)).rowcount
                    if added:
                        self.changes.append(f"{contract} {name}: added {address} in block {int(log['blockNumber'], 16)}")
