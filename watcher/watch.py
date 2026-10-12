import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, NamedTuple

from Crypto.Hash import keccak
from eth_abi.exceptions import DecodingError

from .codes import abi_events, abi_file, register
from .etherscan import Etherscan
from .handlers import decode, run_script
from .rpc import Rpc

CONFIRMATIONS = 12


def topic(signature: str) -> str:
    return "0x" + keccak.new(digest_bits=256, data=signature.encode()).hexdigest()


def word_address(word: str) -> str:
    return "0x" + word[-40:].lower()


class Handler(NamedTuple):
    id: int
    name: str
    kind: str
    signature: str
    script: str
    inputs: list[dict[str, Any]]


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
        watched: dict[str, dict[str, list[Handler]]] = defaultdict(lambda: defaultdict(list))
        rows = self.connection.execute(
            "SELECT d.id, d.contract, d.name, d.kind, e.signature, e.script, c.contract_name, c.source_hash FROM dependency_events e "
            "JOIN dependencies d ON d.id = e.dependency JOIN contracts k ON k.address = d.contract JOIN codes c ON c.id = k.code"
        ).fetchall()
        for id_, contract, name, kind, signature, script, contract_name, source_hash in rows:
            inputs = abi_events(abi_file(self.codes_dir, contract_name, source_hash)).get(signature)
            if inputs is not None:
                watched[contract][topic(signature)].append(Handler(id_, name, kind, signature, script, inputs))
        for contract, topics in watched.items():
            logs = [log for topic0 in sorted(topics) for log in self.etherscan.logs(contract, topic0, start, end)]
            for log in sorted(logs, key=lambda log: (int(log["blockNumber"], 16), int(log["logIndex"] or "0x0", 16))):
                block = int(log["blockNumber"], 16)
                for handler in topics[log["topics"][0]]:
                    try:
                        event = {"address": contract, "signature": handler.signature, "block": block, "transaction": log.get("transactionHash"),
                                 "topics": log["topics"], "data": log["data"], "args": decode(handler.inputs, log)}
                        added, removed = run_script(handler.script, event)
                    except (RuntimeError, DecodingError) as exc:
                        self.changes.append(f"{contract} {handler.name}: script for {handler.signature} failed in block {block}: {exc}")
                        continue
                    self.apply(contract, handler, added, removed, block, hex(end))

    def apply(self, contract: str, handler: Handler, added: list[str], removed: list[str], block: int, tag: str) -> None:
        for address in removed:
            with self.connection:
                gone = self.connection.execute("DELETE FROM dependency_targets WHERE dependency = ? AND address = ?", (handler.id, address)).rowcount
            if gone:
                self.changes.append(f"{contract} {handler.name}: removed {address} in block {block}")
        for address in added:
            if not self.ensure(address, "event", tag):
                self.changes.append(f"{contract} {handler.name}: event added {address}, which is not a contract")
                continue
            with self.connection:
                replaced = 0
                if handler.kind == "single":
                    replaced = self.connection.execute(
                        "DELETE FROM dependency_targets WHERE dependency = ? AND address != ?", (handler.id, address)).rowcount
                inserted = self.connection.execute("INSERT OR IGNORE INTO dependency_targets VALUES (?, ?, 'event')", (handler.id, address)).rowcount
            if replaced or inserted:
                self.changes.append(f"{contract} {handler.name}: {'set to' if handler.kind == 'single' else 'added'} {address} in block {block}")
