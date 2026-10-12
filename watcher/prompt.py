import sqlite3
from pathlib import Path

from .codes import code_folder
from .rpc import ALCHEMY_NETWORKS
from .scope import CHAIN_IDS, Scope

TEMPLATE = Path(__file__).resolve().parent.parent / "DEPENDENCY_PROMPT.md"


def dependency_prompt(connection: sqlite3.Connection, scope: Scope, codes_dir: Path) -> str:
    rows = connection.execute(
        "SELECT k.address, k.name, c.contract_name, c.source_hash FROM contracts k LEFT JOIN codes c ON c.id = k.code "
        "WHERE k.in_scope = 1 ORDER BY k.name, k.address"
    ).fetchall()
    lines = [
        f"- {name} at {address}: "
        + (str(code_folder(codes_dir, contract_name, source_hash)) if source_hash else "no verified source")
        for address, name, contract_name, source_hash in rows
    ]
    return (
        TEMPLATE.read_text()
        .replace("<program>", scope.program)
        .replace("<chain>", scope.chain)
        .replace("<chain_id>", str(CHAIN_IDS[scope.chain]))
        .replace("<network>", ALCHEMY_NETWORKS[scope.chain])
        .replace("<contracts>", "\n".join(lines))
    )
