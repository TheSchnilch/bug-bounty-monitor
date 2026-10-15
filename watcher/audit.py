import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from .agent import run_agent
from .codes import code_folder
from .compare import compare
from .mcp_server import write_mcp_config
from .notify import SEVERITIES
from .rpc import ALCHEMY_NETWORKS
from .scope import CHAIN_IDS, Scope
from .watch import Change

TEMPLATE = Path(__file__).resolve().parent.parent / "AUDIT_PROMPT.md"
FIRST_TEMPLATE = TEMPLATE.with_name("FIRST_AUDIT.md")
CHANGE_TEMPLATE = TEMPLATE.with_name("CHANGE_AUDIT.md")
EFFORT = "xhigh"


def audited(data_root: Path) -> bool:
    return any((data_root / "audits").glob("*/result.json"))


def name_of(connection: sqlite3.Connection, address: str) -> str:
    row = connection.execute("SELECT name FROM contracts WHERE address = ?", (address,)).fetchone()
    return f"{row[0]} ({address})" if row else address


def folder_of(connection: sqlite3.Connection, codes_dir: Path, address: str) -> Path | None:
    row = connection.execute(
        "SELECT c.contract_name, c.source_hash, EXISTS (SELECT 1 FROM contracts s WHERE s.code = c.id AND s.in_scope = 1) "
        "FROM contracts k JOIN codes c ON c.id = k.code WHERE k.address = ?", (address,)
    ).fetchone()
    return code_folder(codes_dir, row[0], row[1], bool(row[2])) if row else None


def change_text(connection: sqlite3.Connection, change: Change) -> str:
    where = f"{change.dependency} of {name_of(connection, change.contract)}"
    if change.kind == "removed" and change.old:
        return f"{where} no longer contains {name_of(connection, change.old)}"
    assert change.new is not None
    if change.kind == "added":
        return f"{where} now also contains {name_of(connection, change.new)}"
    instead = f" instead of {name_of(connection, change.old)}" if change.old else ""
    return f"{where} now points to {name_of(connection, change.new)}{instead}"


def effect(connection: sqlite3.Connection, codes_dir: Path, change: Change, diff: Path) -> str:
    text = change_text(connection, change)
    if change.kind == "removed" or change.new is None:
        return text
    new_folder = folder_of(connection, codes_dir, change.new)
    text += f". Its code: {new_folder}" if new_folder else ". It has no verified source"
    if change.kind == "added" or not change.old:
        return text
    old_folder = folder_of(connection, codes_dir, change.old)
    if old_folder and new_folder and old_folder == new_folder:
        return f"{text}, the same code as before"
    if old_folder and new_folder:
        diff.parent.mkdir(parents=True, exist_ok=True)
        diff.write_text(compare(old_folder, new_folder) + "\n")
        return f"{text}. What changed in the code, without comments and whitespace: {diff}"
    return f"{text}. The previous contract has no verified source"


def by_cause(connection: sqlite3.Connection, changes: list[Change]) -> list[tuple[str, list[Change]]]:
    causes: dict[str, list[Change]] = {}
    for change in changes:
        causes.setdefault(change.cause, []).append(change)
    return [(cause.replace(caused[0].contract, name_of(connection, caused[0].contract), 1), caused) for cause, caused in causes.items()]


def describe(connection: sqlite3.Connection, codes_dir: Path, changes: list[Change], folder: Path) -> list[str]:
    lines: list[str] = []
    diffs = 0
    for number, (cause, caused) in enumerate(by_cause(connection, changes), 1):
        lines.append(f"{number}. {cause}.")
        for change in caused:
            diffs += change.kind == "updated"
            lines.append(f"   - {effect(connection, codes_dir, change, folder / f'diff-{diffs}.diff')}")
    return lines


def summary(connection: sqlite3.Connection, changes: list[Change]) -> list[str]:
    lines: list[str] = []
    for number, (cause, caused) in enumerate(by_cause(connection, changes), 1):
        lines.append(f"**{number}.** {cause}")
        lines += [f"• {change_text(connection, change)}" for change in caused]
    return lines


def findings(folder: Path) -> Counter[str]:
    return Counter(path.name.split("-", 1)[0].lower() for path in (folder / "findings").glob("*.md"))


def findings_text(counts: Counter[str]) -> str:
    parts = [f"{counts[severity]} {severity}" for severity in SEVERITIES if counts[severity]]
    parts += [f"{count} {severity} (unknown severity)" for severity, count in sorted(counts.items()) if severity not in SEVERITIES]
    return f"{sum(counts.values())} findings" + (f": {', '.join(parts)}" if parts else "")


def task(initial: bool, lines: list[str], codes_dir: Path) -> str:
    if initial:
        return FIRST_TEMPLATE.read_text().replace("<codes>", str(codes_dir)).rstrip("\n")
    return CHANGE_TEMPLATE.read_text().replace("<changes>", "\n".join(lines)).rstrip("\n")


def audit_prompt(scope: Scope, data_root: Path, task_text: str, findings: Path) -> str:
    return (
        TEMPLATE.read_text()
        .replace("<program>", scope.program)
        .replace("<chain>", scope.chain)
        .replace("<chain_id>", str(CHAIN_IDS[scope.chain]))
        .replace("<network>", ALCHEMY_NETWORKS[scope.chain])
        .replace("<codes>", str(data_root / "codes"))
        .replace("<dependencies>", str(data_root / "dependencies"))
        .replace("<findings>", str(findings))
        .replace("<task>", task_text)
    )


def run_audit(prompt: str, data_root: Path, folder: Path) -> dict[str, Any]:
    (folder / "findings").mkdir(parents=True, exist_ok=True)
    (folder / "prompt.md").write_text(prompt)
    result = run_agent(prompt, data_root, write_mcp_config(data_root, read_only=True), effort=EFFORT)
    (folder / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
