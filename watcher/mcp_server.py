import argparse
import json
import sqlite3
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from . import db
from .compare import compare
from .db import ADDRESS_PATTERN, address_of
from .codes import abi_events, abi_file, code_folder, register
from .env import REPO_ROOT, etherscan_key, load_env
from .etherscan import Etherscan
from .rpc import Rpc, alchemy_url
from .scope import CHAIN_IDS, load_scope

INSTRUCTIONS = (
    "Tools to inspect and extend the dependency map of one bug bounty program. "
    "Contracts are identified by addresses. Register every contract with add_contract before using it as a dependency target. "
    "Then record dependencies with add_dependency, further list targets with add_dependency_target "
    "and the events that change a dependency with add_dependency_event."
)
READ_ONLY_INSTRUCTIONS = (
    "Read-only view of the dependency map of one bug bounty program: list_contracts, get_contract and list_codes "
    "show every contract, its dependencies and the folder of its cloned code, compare_codes shows how two codes really differ. "
    "Contracts are identified by addresses."
)
CONTRACTS = (
    "SELECT k.address, k.name, k.in_scope, k.origin, EXISTS (SELECT 1 FROM contracts s WHERE s.code = k.code AND s.in_scope = 1), "
    "c.contract_name, c.source_hash FROM contracts k LEFT JOIN codes c ON c.id = k.code"
)


WRITE_TOOLS = ("add_contract", "add_dependency", "add_dependency_target", "add_dependency_event", "remove_dependency")


class ContractInfo(BaseModel):
    address: str
    name: str
    in_scope: bool
    origin: str
    code_folder: str | None


class Event(BaseModel):
    signature: str
    script: str


class Dependency(BaseModel):
    name: str
    slot: str | None
    kind: str
    targets: list[str]
    events: list[Event]


class ContractDetail(ContractInfo):
    dependencies: list[Dependency]


class CodeInfo(BaseModel):
    contract_name: str
    folder: str
    addresses: list[str]


def create_server(data_root: Path, etherscan: Etherscan, rpc: Rpc, read_only: bool = False) -> FastMCP:
    server = FastMCP("watcher", instructions=READ_ONLY_INSTRUCTIONS if read_only else INSTRUCTIONS, log_level="WARNING")
    db_path, codes_dir = data_root / "watcher.db", data_root / "codes"

    @contextmanager
    def connection() -> Generator[sqlite3.Connection, None, None]:
        handle = db.connect(db_path)
        try:
            yield handle
        finally:
            handle.close()

    def contract_info(row: tuple[Any, ...]) -> ContractInfo:
        address, name, in_scope, origin, code_in_scope, contract_name, source_hash = row
        folder = str(code_folder(codes_dir, contract_name, source_hash, bool(code_in_scope))) if source_hash else None
        return ContractInfo(address=address, name=name, in_scope=bool(in_scope), origin=origin, code_folder=folder)

    def registered(handle: sqlite3.Connection, address: str) -> tuple[Any, ...]:
        row = handle.execute(f"{CONTRACTS} WHERE k.address = ?", (address_of(address),)).fetchone()
        if row is None:
            raise ValueError(f"{address} is not registered, call add_contract first")
        return row

    def dependency_of(handle: sqlite3.Connection, contract: str, name: str) -> tuple[int, str]:
        row = handle.execute("SELECT id, kind FROM dependencies WHERE contract = ? AND name = ?", (address_of(contract), name)).fetchone()
        if row is None:
            raise ValueError(f"{contract} has no dependency named {name}")
        return row

    def detail(handle: sqlite3.Connection, address: str) -> ContractDetail:
        info = contract_info(registered(handle, address))
        dependencies = [
            Dependency(
                name=name, slot=slot, kind=kind,
                targets=[row[0] for row in handle.execute("SELECT address FROM dependency_targets WHERE dependency = ? ORDER BY address", (id_,))],
                events=[Event(signature=s, script=script) for s, script in handle.execute(
                    "SELECT signature, script FROM dependency_events WHERE dependency = ? ORDER BY signature", (id_,))],
            )
            for id_, name, slot, kind in handle.execute(
                "SELECT id, name, slot, kind FROM dependencies WHERE contract = ? ORDER BY name", (info.address,)).fetchall()
        ]
        return ContractDetail(**info.model_dump(), dependencies=dependencies)

    @server.tool()
    def list_contracts() -> list[ContractInfo]:
        """List every contract of the program with its name, scope flag, origin and the folder of its cloned code."""
        with connection() as handle:
            return [contract_info(row) for row in handle.execute(f"{CONTRACTS} ORDER BY k.name, k.address")]

    @server.tool()
    def get_contract(address: str) -> ContractDetail:
        """Show one contract with its dependencies, their targets and the events that update them."""
        with connection() as handle:
            return detail(handle, address)

    @server.tool()
    def list_codes() -> list[CodeInfo]:
        """List every unique verified code with its folder and the addresses that run it."""
        with connection() as handle:
            rows = handle.execute(
                "SELECT c.contract_name, c.source_hash, group_concat(k.address), max(k.in_scope) FROM codes c JOIN contracts k ON k.code = c.id "
                "GROUP BY c.id ORDER BY c.contract_name"
            ).fetchall()
        return [
            CodeInfo(contract_name=name, folder=str(code_folder(codes_dir, name, source_hash, bool(in_scope))), addresses=sorted(addresses.split(",")))
            for name, source_hash, addresses, in_scope in rows
        ]

    @server.tool()
    def compare_codes(first: str, second: str) -> str:
        """Show how two codes really differ: a diff that ignores comments and whitespace. Flattened sources are compared file by file,
        so the result names the original files that changed. Each argument is a contract address or a code folder such as
        MarketManagerIsolated-9b869873."""
        with connection() as handle:
            return compare(code_path(handle, first), code_path(handle, second))

    def code_path(handle: sqlite3.Connection, value: str) -> Path:
        in_scope = "EXISTS (SELECT 1 FROM contracts s WHERE s.code = c.id AND s.in_scope = 1)"
        if ADDRESS_PATTERN.match(value):
            row = handle.execute(f"SELECT c.contract_name, c.source_hash, {in_scope} FROM contracts k JOIN codes c ON c.id = k.code WHERE k.address = ?",
                                 (value.lower(),)).fetchone()
        else:
            name, _, prefix = Path(value).name.rpartition("-")
            row = handle.execute(f"SELECT c.contract_name, c.source_hash, {in_scope} FROM codes c WHERE c.contract_name = ? AND substr(c.source_hash, 1, 8) = ?",
                                 (name, prefix)).fetchone()
        if row is None:
            raise ValueError(f"{value} is neither a contract with verified source nor a code folder")
        return code_folder(codes_dir, row[0], row[1], bool(row[2]))

    @server.tool()
    def add_contract(address: str, name: str) -> ContractInfo:
        """Register a contract the program depends on. Its verified source is cloned into its code folder.
        A contract that is already registered is returned unchanged."""
        address = address_of(address)
        with connection() as handle:
            row = handle.execute(f"{CONTRACTS} WHERE k.address = ?", (address,)).fetchone()
            if row is not None:
                return contract_info(row)
            if rpc.call("eth_getCode", address, "latest") in ("0x", "0x0", None):
                raise ValueError(f"no contract is deployed at {address}")
            register(handle, etherscan, codes_dir, address, name, "agent")
            return contract_info(registered(handle, address))

    @server.tool()
    def add_dependency(contract: str, name: str, kind: Literal["single", "list"], targets: list[str], slot: str | None = None) -> ContractDetail:
        """Record that a contract depends on registered target contracts. A single dependency has exactly one target.
        Give the storage slot when the address is stored in a storage variable; the slot must hold the target now.
        Leave the slot out for immutables, constants and lists."""
        targets = sorted({address_of(target) for target in targets})
        with connection() as handle:
            owner = contract_info(registered(handle, contract))
            for target in targets:
                registered(handle, target)
            if kind == "single" and len(targets) != 1:
                raise ValueError("a single dependency needs exactly one target")
            if slot is not None:
                if kind != "single":
                    raise ValueError("only a single dependency can have a slot")
                slot = hex(int(slot, 16))
                stored = "0x" + str(rpc.call("eth_getStorageAt", owner.address, slot, "latest"))[-40:].lower()
                if stored != targets[0]:
                    raise ValueError(f"slot {slot} of {owner.address} holds {stored}, not {targets[0]}")
            with handle:
                if handle.execute("SELECT 1 FROM dependencies WHERE contract = ? AND name = ?", (owner.address, name)).fetchone():
                    raise ValueError(f"{owner.address} already has a dependency named {name}")
                id_ = handle.execute(
                    "INSERT INTO dependencies (contract, name, slot, kind) VALUES (?, ?, ?, ?)", (owner.address, name, slot, kind)
                ).lastrowid
                handle.executemany("INSERT INTO dependency_targets VALUES (?, ?, 'agent')", [(id_, target) for target in targets])
            return detail(handle, owner.address)

    @server.tool()
    def add_dependency_target(contract: str, dependency: str, address: str) -> ContractDetail:
        """Add a registered contract to a list dependency."""
        with connection() as handle:
            id_, kind = dependency_of(handle, contract, dependency)
            target = contract_info(registered(handle, address)).address
            if kind != "list":
                raise ValueError(f"{dependency} is a single dependency, remove and add it again to change its target")
            with handle:
                handle.execute("INSERT OR IGNORE INTO dependency_targets VALUES (?, ?, 'agent')", (id_, target))
            return detail(handle, contract)

    @server.tool()
    def add_dependency_event(contract: str, dependency: str, signature: str, script: str) -> ContractDetail:
        """Record an event of the contract that changes the dependency, with a small Python script that interprets it.
        The signature is the canonical form, for example PositionManagerUpdated(address,bool).
        The script defines handle(event) and returns two lists: (added addresses, removed addresses).
        event has address, signature, block, transaction, topics, data and args, the parameters decoded by name
        (addresses lowercase, bytes as hex, tuples and arrays as lists). For example:
            def handle(event):
                if event["args"]["addPerms"]:
                    return [event["args"]["positionManager"]], []
                return [], [event["args"]["positionManager"]]
        For a single dependency the added address replaces the target. Saving the same event again replaces its script."""
        with connection() as handle:
            id_, _ = dependency_of(handle, contract, dependency)
            *_, contract_name, source_hash = registered(handle, contract)
            if source_hash is None:
                raise ValueError(f"{contract} has no verified source, its events cannot be decoded")
            events = abi_events(abi_file(codes_dir, contract_name, source_hash))
            if signature not in events:
                raise ValueError(f"{contract} has no event {signature}, its events are: {', '.join(sorted(events))}")
            with handle:
                handle.execute("INSERT OR REPLACE INTO dependency_events VALUES (?, ?, ?)", (id_, signature, script))
            return detail(handle, contract)

    @server.tool()
    def remove_dependency(contract: str, dependency: str) -> ContractDetail:
        """Remove a dependency with its targets and events. The target contracts stay registered."""
        with connection() as handle:
            id_, _ = dependency_of(handle, contract, dependency)
            with handle:
                handle.execute("DELETE FROM dependencies WHERE id = ?", (id_,))
            return detail(handle, contract)

    if read_only:
        for name in WRITE_TOOLS:
            server.remove_tool(name)
    return server


def write_mcp_config(data_root: Path, read_only: bool = False) -> Path:
    server = {
        "command": sys.executable,
        "args": ["-m", "watcher.mcp_server", "--data-root", str(data_root), *(["--read-only"] if read_only else [])],
        "env": {"PYTHONPATH": str(REPO_ROOT)},
    }
    path = data_root / ("mcp-audit.json" if read_only else "mcp.json")
    path.write_text(json.dumps({"mcpServers": {"watcher": server}}, indent=2) + "\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(prog="watcher.mcp_server", description="MCP server for the dependency map of one program")
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--read-only", action="store_true", help="only offer the tools that read the map")
    args = parser.parse_args()
    data_root = Path(args.data_root).resolve()
    if not (data_root / "watcher.db").exists():
        raise SystemExit(f"{data_root / 'watcher.db'} does not exist; start the watcher first")
    scope = load_scope(data_root / "scope.json")
    env = load_env()
    etherscan = Etherscan(etherscan_key(env), CHAIN_IDS[scope.chain])
    create_server(data_root, etherscan, Rpc(alchemy_url(env, scope.chain)), args.read_only).run("stdio")


if __name__ == "__main__":
    main()
