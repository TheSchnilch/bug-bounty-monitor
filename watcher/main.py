import argparse
import sqlite3
import time
import traceback
from pathlib import Path

from . import db
from .agent import EFFORT, MODEL, run_agent
from .audit import EFFORT as AUDIT_EFFORT
from .audit import audit_prompt, audited, describe, findings, findings_text, run_audit, summary, task
from .codes import clone_codes, remove_codes
from .env import etherscan_key, load_env
from .etherscan import Code, Etherscan
from .mcp_server import write_mcp_config
from .notify import audit_finished_embed, audit_started_embed, changes_embed, cycle_failed_embed, cycle_recovered_embed, redacted, send
from .prompt import dependency_prompt
from .rpc import Rpc, alchemy_url
from .scope import CHAIN_IDS, Scope, load_scope
from .watch import Watch

INTERVAL = 300


def fill(connection: sqlite3.Connection, scope: Scope, etherscan: Etherscan) -> list[str]:
    stored: dict[str, bool] = {address: bool(verified) for address, verified in connection.execute("SELECT address, code IS NOT NULL FROM contracts")}
    known = {address for address, verified in stored.items() if verified}
    fetched: list[tuple[str, str, Code | None]] = []
    for contract in scope.contracts:
        address = contract.address.lower()
        code = None
        if address not in stored:
            code = etherscan.code(address)
        elif address not in known:
            try:
                code = etherscan.code(address)
            except RuntimeError as exc:
                say(f"source of the unverified {address} not checked, trying again next cycle: {exc}")
        fetched.append((address, contract.name, code))
    with connection:
        for address, name, code in fetched:
            db.save_contract(connection, address, name, code)
    return [address for address, _, code in fetched if code is None and address not in known]


def say(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


def tidy(connection: sqlite3.Connection, data_root: Path, scope: Scope, etherscan: Etherscan) -> None:
    with connection:
        pruned = db.prune(connection, [contract.address.lower() for contract in scope.contracts])
    remove_codes(data_root / "codes", pruned.codes)
    cloned = clone_codes(connection, etherscan, data_root / "codes")
    for label, items in (
        ("removed from scope", pruned.out_of_scope),
        ("deleted contracts", pruned.deleted),
        ("deleted codes", [f"{name}-{source_hash[:8]}" for name, source_hash in pruned.codes]),
        ("cloned", [folder.name for folder in cloned]),
    ):
        if items:
            say(f"{label}: {', '.join(items)}")


def sync(connection: sqlite3.Connection, data_root: Path, scope: Scope, etherscan: Etherscan) -> None:
    in_scope = {row[0] for row in connection.execute("SELECT address FROM contracts WHERE in_scope = 1")}
    added = sorted({contract.address.lower() for contract in scope.contracts} - in_scope)
    unverified = fill(connection, scope, etherscan)
    tidy(connection, data_root, scope, etherscan)
    for label, items in (("new in scope", added), ("without verified source", [address for address in unverified if address in added])):
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
        watch = Watch(connection, Rpc(alchemy_url(env, scope.chain)), etherscan, data_root / "codes")
        block, changes = watch.run()
        for change in changes:
            say(change)
        initial = not audited(data_root)
        folder = data_root / "audits" / f"{time.strftime('%Y%m%d-%H%M%S')}-block{block}"
        updates = watch.updates
        lines = describe(connection, data_root / "codes", updates, folder)
        if watch.updates:
            diffs = sorted(folder.glob("diff-*.diff"))
            send(env, changes_embed(scope.program, scope.chain, block, len(updates), summary(connection, updates), len(diffs)), diffs)
        tidy(connection, data_root, scope, etherscan)
        total, in_scope, links = connection.execute(
            "SELECT (SELECT count(*) FROM contracts), (SELECT count(*) FROM contracts WHERE in_scope = 1), (SELECT count(*) FROM dependencies)"
        ).fetchone()
        say(f"checked up to block {block}: {total} contracts ({in_scope} in scope, {total - in_scope} dependencies), {links} dependency links, {len(changes)} changes")
    finally:
        connection.close()
    if initial or lines:
        prompt = audit_prompt(scope, data_root, task(initial, lines, data_root / "codes"), folder / "findings")
        reason = "first audit of everything in scope" if initial else f"{len(updates)} dependency changes"
        say(f"running the audit ({reason}, {MODEL}, effort {AUDIT_EFFORT}) into {folder}")
        send(env, audit_started_embed(scope.program, reason, block, MODEL, AUDIT_EFFORT, folder))
        result = run_audit(prompt, data_root, folder)
        counts = findings(folder)
        say(f"audit finished with {findings_text(counts)}, session {result.get('session_id')}, ${result.get('total_cost_usd', 0):.2f}:\n{result.get('result')}")
        names = sorted(path.name for path in (folder / "findings").glob("*.md"))
        send(env, audit_finished_embed(scope.program, counts, int(result.get("duration_ms", 0)) // 60000, float(result.get("total_cost_usd", 0)), names, folder))


class Loop:
    def __init__(self, data_root: Path, env: dict[str, str]) -> None:
        self.data_root, self.env = data_root, env
        self.failures, self.last_error = 0, ""

    def step(self) -> None:
        try:
            cycle(self.data_root, self.env)
        except Exception as exc:
            error, trace = redacted(f"{type(exc).__name__}: {exc}", self.env), redacted(traceback.format_exc(), self.env)
            self.failures += 1
            say(f"cycle failed: {error}\n{trace}")
            if error != self.last_error:
                send(self.env, cycle_failed_embed(self.data_root.name, error, trace, self.failures))
            self.last_error = error
            return
        if self.failures:
            send(self.env, cycle_recovered_embed(self.data_root.name, self.failures))
        self.failures, self.last_error = 0, ""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="watcher", description="Watch the contracts of a bug bounty scope and their dependencies")
    parser.add_argument("--data-root", required=True, help="folder of one program, containing scope.json")
    loop = Loop(Path(parser.parse_args(argv).data_root).resolve(), load_env())
    while True:
        started = time.monotonic()
        loop.step()
        time.sleep(max(0.0, INTERVAL - (time.monotonic() - started)))
