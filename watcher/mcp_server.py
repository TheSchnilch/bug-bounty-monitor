import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from . import db
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
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
CONTRACTS = (
    "SELECT k.address, k.name, k.in_scope, k.origin, c.contract_name, c.source_hash "
    "FROM contracts k LEFT JOIN codes c ON c.id = k.code"
)


class ContractInfo(BaseModel):
    address: str
    name: str
    in_scope: bool
    origin: str
    code_folder: str | None


class Event(BaseModel):
    signature: str
    field: str


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


def address_of(value: str) -> str:
    if not ADDRESS.match(value):
        raise ValueError(f"{value} is not an address")
    return value.lower()


def create_server(data_root: Path, etherscan: Etherscan, rpc: Rpc) -> FastMCP:
    server = FastMCP("watcher", instructions=INSTRUCTIONS, log_level="WARNING")
    db_path, codes_dir = data_root / "watcher.db", data_root / "codes"

    @contextmanager
    def connection() -> Generator[sqlite3.Connection, None, None]:
        handle = db.connect(db_path)
        try:
            yield handle
        finally:
            handle.close()

    def folder_of(contract_name: str | None, source_hash: str | None) -> Path | None:
        return code_folder(codes_dir, contract_name, source_hash) if contract_name and source_hash else None

    def contract_info(row: tuple[Any, ...]) -> ContractInfo:
        address, name, in_scope, origin, contract_name, source_hash = row
        folder = folder_of(contract_name, source_hash)
        return ContractInfo(address=address, name=name, in_scope=bool(in_scope), origin=origin, code_folder=str(folder) if folder else None)

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
                events=[Event(signature=s, field=f) for s, f in handle.execute(
                    "SELECT signature, field FROM dependency_events WHERE dependency = ? ORDER BY signature", (id_,))],
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
                "SELECT c.contract_name, c.source_hash, group_concat(k.address) FROM codes c JOIN contracts k ON k.code = c.id "
                "GROUP BY c.id ORDER BY c.contract_name"
            ).fetchall()
        return [
            CodeInfo(contract_name=name, folder=str(code_folder(codes_dir, name, source_hash)), addresses=sorted(addresses.split(",")))
            for name, source_hash, addresses in rows
        ]

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
    def add_dependency_event(contract: str, dependency: str, signature: str, field: str) -> ContractDetail:
        """Record an event of the contract that changes the dependency. The signature is the canonical form,
        for example TokenListed(address,uint256), and field is the name of the address parameter holding the new target."""
        with connection() as handle:
            id_, _ = dependency_of(handle, contract, dependency)
            *_, contract_name, source_hash = registered(handle, contract)
            if source_hash is None:
                raise ValueError(f"{contract} has no verified source, its events cannot be checked")
            events = abi_events(abi_file(codes_dir, contract_name, source_hash))
            if signature not in events:
                raise ValueError(f"{contract} has no event {signature}, its events are: {', '.join(sorted(events))}")
            fields = [str(i["name"]) for i in events[signature] if i["type"] == "address"]
            if field not in fields:
                raise ValueError(f"{signature} has no address parameter {field}, its address parameters are: {', '.join(fields)}")
            with handle:
                handle.execute("INSERT OR REPLACE INTO dependency_events VALUES (?, ?, ?)", (id_, signature, field))
            return detail(handle, contract)

    @server.tool()
    def remove_dependency(contract: str, dependency: str) -> ContractDetail:
        """Remove a dependency with its targets and events. The target contracts stay registered."""
        with connection() as handle:
            id_, _ = dependency_of(handle, contract, dependency)
            with handle:
                handle.execute("DELETE FROM dependencies WHERE id = ?", (id_,))
            return detail(handle, contract)

    return server


def write_mcp_config(data_root: Path) -> Path:
    server = {
        "command": sys.executable,
        "args": ["-m", "watcher.mcp_server", "--data-root", str(data_root)],
        "env": {"PYTHONPATH": str(REPO_ROOT)},
    }
    path = data_root / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"watcher": server}}, indent=2) + "\n")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(prog="watcher.mcp_server", description="MCP server for the dependency map of one program")
    parser.add_argument("--data-root", required=True)
    data_root = Path(parser.parse_args().data_root).resolve()
    if not (data_root / "watcher.db").exists():
        raise SystemExit(f"{data_root / 'watcher.db'} does not exist; run init first")
    scope = load_scope(data_root / "scope.json")
    env = load_env()
    etherscan = Etherscan(etherscan_key(env), CHAIN_IDS[scope.chain])
    create_server(data_root, etherscan, Rpc(alchemy_url(env, scope.chain))).run("stdio")


if __name__ == "__main__":
    main()
