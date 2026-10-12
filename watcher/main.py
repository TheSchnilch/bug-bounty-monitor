import argparse
import sqlite3
import time
from pathlib import Path

from . import db
from .agent import EFFORT, MODEL, run_agent
from .codes import clone_codes, remove_codes
from .env import etherscan_key, load_env
from .etherscan import Etherscan
from .mcp_server import write_mcp_config
from .prompt import dependency_prompt
from .rpc import Rpc, alchemy_url
from .scope import CHAIN_IDS, Scope, load_scope
from .watch import Watch

INTERVAL = 300


def fill(connection: sqlite3.Connection, scope: Scope, etherscan: Etherscan) -> list[str]:
    fetched = [(contract.address.lower(), contract.name, etherscan.code(contract.address.lower())) for contract in scope.contracts]
    with connection:
        for address, name, code in fetched:
            db.save_contract(connection, address, name, code)
    return [address for address, _, code in fetched if code is None]


def say(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def sync(connection: sqlite3.Connection, data_root: Path, scope: Scope, etherscan: Etherscan) -> None:
    in_scope = {row[0] for row in connection.execute("SELECT address FROM contracts WHERE in_scope = 1")}
    added = sorted({contract.address.lower() for contract in scope.contracts} - in_scope)
    unverified = fill(connection, scope, etherscan)
    with connection:
        pruned = db.prune(connection, [contract.address.lower() for contract in scope.contracts])
    remove_codes(data_root / "codes", pruned.codes)
    cloned = clone_codes(connection, etherscan, data_root / "codes")
    for label, items in (
        ("removed from scope", pruned.out_of_scope),
        ("deleted contracts", pruned.deleted),
        ("deleted codes", [f"{name}-{source_hash[:8]}" for name, source_hash in pruned.codes]),
        ("new in scope", added),
        ("without verified source", [address for address in unverified if address in added]),
        ("cloned", [folder.name for folder in cloned]),
    ):
        if items:
            say(f"{label}: {', '.join(items)}")
    if added:
        prompt = dependency_prompt(connection, scope, data_root / "codes")
        say(f"running the dependency agent ({MODEL}, effort {EFFORT}) with this prompt:\n{prompt}")
        result = run_agent(prompt, data_root, write_mcp_config(data_root))
        say(f"dependency agent finished, session {result.get('session_id')}, ${result.get('total_cost_usd', 0):.2f}:\n{result.get('result')}")


def cycle(data_root: Path, env: dict[str, str]) -> None:
    scope = load_scope(data_root / "scope.json")
    etherscan = Etherscan(etherscan_key(env), CHAIN_IDS[scope.chain])
    connection = db.init(data_root / "watcher.db")
    try:
        sync(connection, data_root, scope, etherscan)
        block, changes = Watch(connection, Rpc(alchemy_url(env, scope.chain)), etherscan, data_root / "codes").run()
        counts = connection.execute("SELECT (SELECT count(*) FROM contracts), (SELECT count(*) FROM dependencies)").fetchone()
    finally:
        connection.close()
    for change in changes:
        say(change)
    say(f"checked up to block {block}: {counts[0]} contracts, {counts[1]} dependencies, {len(changes)} changes")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="watcher", description="Watch the contracts of a bug bounty scope and their dependencies")
    parser.add_argument("--data-root", required=True, help="folder of one program, containing scope.json")
    data_root = Path(parser.parse_args(argv).data_root).resolve()
    env = load_env()
    while True:
        started = time.monotonic()
        try:
            cycle(data_root, env)
        except (RuntimeError, OSError, ValueError) as exc:
            say(f"cycle failed: {exc}")
        time.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))
