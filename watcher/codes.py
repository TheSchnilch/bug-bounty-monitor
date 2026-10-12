import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import db
from .etherscan import Etherscan, code_of, source_files


def code_folder(codes_dir: Path, contract_name: str, source_hash: str, in_scope: bool) -> Path:
    return (codes_dir if in_scope else codes_dir.parent / "dependencies") / f"{contract_name}-{source_hash[:8]}"


def abi_file(codes_dir: Path, contract_name: str, source_hash: str) -> Path:
    return codes_dir.parent / "abis" / f"{contract_name}-{source_hash[:8]}.json"


def clone_codes(connection: sqlite3.Connection, etherscan: Etherscan, codes_dir: Path) -> list[Path]:
    rows = connection.execute(
        "SELECT c.source_hash, c.contract_name, min(k.address), max(k.in_scope) FROM codes c JOIN contracts k ON k.code = c.id GROUP BY c.id ORDER BY c.id"
    ).fetchall()
    cloned: list[Path] = []
    for source_hash, contract_name, address, in_scope in rows:
        folder, abi = code_folder(codes_dir, contract_name, source_hash, bool(in_scope)), abi_file(codes_dir, contract_name, source_hash)
        elsewhere = code_folder(codes_dir, contract_name, source_hash, not in_scope)
        if elsewhere.exists() and not folder.exists():
            folder.parent.mkdir(parents=True, exist_ok=True)
            elsewhere.rename(folder)
        if folder.exists() and abi.exists():
            continue
        entry = etherscan.source(address)
        code = code_of(entry)
        if code is None or code.source_hash != source_hash:
            raise RuntimeError(f"source of {address} no longer matches code {contract_name} ({source_hash})")
        if not folder.exists():
            partial = folder.with_name(folder.name + ".partial")
            shutil.rmtree(partial, ignore_errors=True)
            for path, content in source_files(entry).items():
                target = partial / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            partial.rename(folder)
            cloned.append(folder)
        abi.parent.mkdir(parents=True, exist_ok=True)
        abi.write_text(json.dumps(json.loads(entry["ABI"]), indent=2) + "\n")
    return cloned


def remove_codes(codes_dir: Path, codes: list[tuple[str, str]]) -> None:
    for contract_name, source_hash in codes:
        for in_scope in (True, False):
            shutil.rmtree(code_folder(codes_dir, contract_name, source_hash, in_scope), ignore_errors=True)
        abi_file(codes_dir, contract_name, source_hash).unlink(missing_ok=True)


def register(connection: sqlite3.Connection, etherscan: Etherscan, codes_dir: Path, address: str, name: str | None, origin: str) -> str:
    code = etherscan.code(address)
    name = name or (code.contract_name if code else address)
    with connection:
        code_id = db.save_code(connection, code) if code else None
        connection.execute("INSERT INTO contracts (address, name, in_scope, origin, code) VALUES (?, ?, 0, ?, ?)", (address, name, origin, code_id))
    clone_codes(connection, etherscan, codes_dir)
    return name


def canonical_type(item: dict[str, Any]) -> str:
    kind = str(item["type"])
    if kind.startswith("tuple"):
        return "(" + ",".join(canonical_type(component) for component in item["components"]) + ")" + kind[5:]
    return kind


def abi_events(path: Path) -> dict[str, list[dict[str, Any]]]:
    abi: list[dict[str, Any]] = json.loads(path.read_text())
    return {
        f"{item['name']}({','.join(canonical_type(i) for i in item['inputs'])})": item["inputs"]
        for item in abi if item.get("type") == "event"
    }
