import json
import re
import sqlite3
from pathlib import Path
from typing import NamedTuple

SCHEMA_VERSION = 6
ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")
ADDRESS = "length({0}) = 42 AND substr({0}, 1, 2) = '0x' AND substr({0}, 3) NOT GLOB '*[^0-9a-f]*'"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS codes (
    id INTEGER PRIMARY KEY,
    source_hash TEXT NOT NULL UNIQUE CHECK (length(source_hash) = 64 AND source_hash NOT GLOB '*[^0-9a-f]*'),
    contract_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    address TEXT PRIMARY KEY CHECK ({ADDRESS.format("address")}),
    name TEXT NOT NULL,
    in_scope INTEGER NOT NULL CHECK (in_scope IN (0, 1)),
    origin TEXT NOT NULL CHECK (origin IN ('manual', 'agent', 'event', 'slot')),
    code INTEGER REFERENCES codes (id)
);

CREATE TABLE IF NOT EXISTS dependencies (
    id INTEGER PRIMARY KEY,
    contract TEXT NOT NULL REFERENCES contracts (address) ON DELETE CASCADE,
    name TEXT NOT NULL,
    slot TEXT CHECK (slot IS NULL OR (substr(slot, 1, 2) = '0x' AND length(slot) BETWEEN 3 AND 66)),
    kind TEXT NOT NULL CHECK (kind IN ('single', 'list')),
    UNIQUE (contract, name)
);

CREATE TABLE IF NOT EXISTS dependency_targets (
    dependency INTEGER NOT NULL REFERENCES dependencies (id) ON DELETE CASCADE,
    address TEXT NOT NULL REFERENCES contracts (address),
    source TEXT NOT NULL CHECK (source IN ('manual', 'agent', 'event', 'slot')),
    PRIMARY KEY (dependency, address)
);

CREATE TABLE IF NOT EXISTS dependency_events (
    dependency INTEGER NOT NULL REFERENCES dependencies (id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    script TEXT NOT NULL,
    PRIMARY KEY (dependency, signature)
);

CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    block INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS contracts_by_code ON contracts (code);
CREATE INDEX IF NOT EXISTS dependency_targets_by_address ON dependency_targets (address);
"""

REACHABLE = (
    "WITH RECURSIVE reachable (address) AS (SELECT address FROM contracts WHERE in_scope = 1 "
    "UNION SELECT t.address FROM reachable r JOIN dependencies d ON d.contract = r.address JOIN dependency_targets t ON t.dependency = d.id)"
)


class Pruned(NamedTuple):
    out_of_scope: list[str]
    deleted: list[str]
    codes: list[tuple[str, str]]


def address_of(value: str) -> str:
    if not ADDRESS_PATTERN.match(value):
        raise ValueError(f"{value} is not an address")
    return value.lower()


def connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = connect(path)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise RuntimeError(f"{path} has schema version {version}, this tool expects {SCHEMA_VERSION}")
    with connection:
        connection.executescript(SCHEMA)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return connection


def save_code(connection: sqlite3.Connection, code: tuple[str, str]) -> int:
    return connection.execute(
        "INSERT INTO codes (source_hash, contract_name) VALUES (?, ?) "
        "ON CONFLICT (source_hash) DO UPDATE SET contract_name = excluded.contract_name RETURNING id",
        code,
    ).fetchone()[0]


def save_contract(connection: sqlite3.Connection, address: str, name: str, code: tuple[str, str] | None) -> None:
    connection.execute(
        "INSERT INTO contracts (address, name, in_scope, origin) VALUES (?, ?, 1, 'manual') "
        "ON CONFLICT (address) DO UPDATE SET name = excluded.name, in_scope = 1, origin = 'manual'",
        (address, name),
    )
    if code is not None:
        connection.execute("UPDATE contracts SET code = ? WHERE address = ?", (save_code(connection, code), address))


def prune(connection: sqlite3.Connection, scope: list[str]) -> Pruned:
    out_of_scope = connection.execute(
        "UPDATE contracts SET in_scope = 0 WHERE in_scope = 1 AND address NOT IN (SELECT value FROM json_each(?)) RETURNING address",
        (json.dumps(scope),),
    ).fetchall()
    connection.execute(f"{REACHABLE} DELETE FROM dependencies WHERE contract NOT IN (SELECT address FROM reachable)")
    deleted = connection.execute(f"{REACHABLE} DELETE FROM contracts WHERE address NOT IN (SELECT address FROM reachable) RETURNING address").fetchall()
    codes = connection.execute(
        "DELETE FROM codes WHERE id NOT IN (SELECT code FROM contracts WHERE code IS NOT NULL) RETURNING contract_name, source_hash"
    ).fetchall()
    return Pruned(sorted(row[0] for row in out_of_scope), sorted(row[0] for row in deleted), sorted(codes))
