import hashlib
import json
import re
import shutil
import time
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

from .sources import fetch_json

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
ADDRESS_FOLDER = re.compile(r"^(?P<name>.+)-(?P<address>0x[0-9a-fA-F]{40})$")
ADDRESSES_DIR = "addresses"


class Contract(NamedTuple):
    address: str
    name: str
    files: dict[str, str]


class Etherscan:
    def __init__(self, key: str, chain_id: int, url: str = ETHERSCAN_URL, min_interval: float = 0.25, attempts: int = 3) -> None:
        self.key, self.chain_id, self.url, self.min_interval, self.attempts = key, chain_id, url, min_interval, attempts
        self.last = 0.0

    def source(self, address: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"chainid": self.chain_id, "module": "contract", "action": "getsourcecode", "address": address, "apikey": self.key})
        error: object = None
        for attempt in range(self.attempts):
            time.sleep(max(2.0 * attempt, self.last + self.min_interval - time.monotonic()))
            self.last = time.monotonic()
            try:
                data = fetch_json(f"{self.url}?{query}")
                if data.get("status") == "1" and data.get("result"):
                    return data["result"][0]
                error = data.get("result") or data.get("message")
            except (OSError, ValueError) as exc:
                error = exc
        raise RuntimeError(f"Etherscan getsourcecode for {address} failed: {error}")


def clean_path(path: str) -> str:
    return "/".join(part for part in PurePosixPath(path).parts if part not in ("/", ".", ".."))


def source_files(entry: dict[str, Any]) -> dict[str, str]:
    code = str(entry.get("SourceCode") or "")
    if code.startswith("{{"):
        sources = json.loads(code[1:-1])["sources"]
    elif code.startswith("{"):
        parsed = json.loads(code)
        sources = parsed.get("sources", parsed)
    else:
        extension = ".vy" if "vyper" in str(entry.get("CompilerVersion", "")).lower() else ".sol"
        return {f"{entry.get('ContractName') or 'Contract'}{extension}": code}
    return {clean_path(path): item["content"] for path, item in sources.items()}


def read_files(folder: Path) -> dict[str, str]:
    return {str(path.relative_to(folder)): path.read_text() for path in sorted(folder.rglob("*")) if path.is_file()}


def load_contracts(project: Path) -> tuple[dict[str, Contract], list[Path]]:
    contracts: dict[str, Contract] = {}
    folders: list[Path] = []
    for folder in sorted(project.iterdir()) if project.exists() else []:
        if not folder.is_dir() or folder.name.startswith(".") or folder.name == ADDRESSES_DIR:
            continue
        match = ADDRESS_FOLDER.match(folder.name)
        listing = project / ADDRESSES_DIR / f"{folder.name}.txt"
        if listing.exists():
            addresses = listing.read_text().split()
        elif match:
            addresses = [match.group("address")]
        else:
            continue
        folders.append(folder)
        files = read_files(folder)
        name = match.group("name") if match else folder.name
        for address in addresses:
            contracts[address.lower()] = Contract(address, name, files)
    return contracts, folders


def write_layout(project: Path, contracts: dict[str, Contract], old_folders: list[Path]) -> None:
    for folder in old_folders:
        shutil.rmtree(folder)
    shutil.rmtree(project / ADDRESSES_DIR, ignore_errors=True)
    groups: dict[tuple[str, str], list[Contract]] = defaultdict(list)
    for contract in contracts.values():
        content = hashlib.sha256(json.dumps(contract.files, sort_keys=True).encode()).hexdigest()
        groups[(contract.name, content)].append(contract)
    versions = Counter(name for name, _ in groups)
    for (name, _), members in sorted(groups.items()):
        members.sort(key=lambda contract: contract.address.lower())
        if len(members) == 1:
            folder = f"{name}-{members[0].address}"
        else:
            folder = name if versions[name] == 1 else f"{name}-{members[0].address}"
            (project / ADDRESSES_DIR).mkdir(parents=True, exist_ok=True)
            (project / ADDRESSES_DIR / f"{folder}.txt").write_text("".join(f"{contract.address}\n" for contract in members))
        for path, content in members[0].files.items():
            target = project / folder / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)


def clone(etherscan: Etherscan, addresses: list[str], project: Path) -> tuple[list[str], list[str]]:
    contracts, old_folders = load_contracts(project)
    seen: set[str] = set()
    cloned: list[str] = []
    unverified: list[str] = []
    for address in addresses:
        if address.lower() in seen:
            continue
        seen.add(address.lower())
        entry = etherscan.source(address)
        if not str(entry.get("SourceCode") or "").strip():
            unverified.append(address)
            continue
        contracts[address.lower()] = Contract(address, str(entry.get("ContractName") or "Contract"), source_files(entry))
        cloned.append(address)
    write_layout(project, contracts, old_folders)
    return cloned, unverified
