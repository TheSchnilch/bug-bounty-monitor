import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

from .models import Asset, Fingerprint

ADDRESS_URL = re.compile(r"^https?://(?:www\.)?etherscan\.io/(?:address|token)/(0x[0-9a-fA-F]{40})/?(?:[?#].*)?$")


def fetch_json(url: str, payload: object = None) -> Any:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data, headers={"User-Agent": "watch-code/0.1", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.loads(response.read())


def load_program(source: str, slug: str) -> list[Asset]:
    bounties = fetch_json(source) if source.startswith(("http://", "https://")) else json.loads(Path(source).read_text())
    programs = [bounty for bounty in bounties if bounty.get("slug") == slug]
    if len(programs) != 1 or programs[0].get("isPaused"):
        raise ValueError(f"Immunefi program {slug!r} is missing, ambiguous or paused")
    assets: list[Asset] = []
    for asset in programs[0]["assets"]:
        match = ADDRESS_URL.match(asset["url"].strip())
        if match and asset["type"] == "smart_contract":
            assets.append(Asset(name=(asset.get("description") or "").strip(), url=asset["url"], address=match.group(1).lower()))
    return assets


class Rpc:
    def __init__(self, url: str, attempts: int = 3) -> None:
        self.url, self.attempts = url, attempts

    def __call__(self, method: str, *params: object) -> Any:
        error: object = None
        for attempt in range(self.attempts):
            time.sleep(2.0 * attempt)
            try:
                data = fetch_json(self.url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)})
                if "result" in data:
                    return data["result"]
                error = data.get("error")
            except (OSError, ValueError) as exc:
                error = exc
        raise RuntimeError(f"RPC {method} failed: {error}")


def to_address(word: str) -> str | None:
    value = int(word[2:] or "0", 16) % (1 << 160)
    return f"0x{value:040x}" if value else None


def fingerprint(rpc: Rpc, address: str, slot: str | None, block: int) -> Fingerprint:
    tag = hex(block)
    implementation = to_address(rpc("eth_getStorageAt", address, slot, tag)) if slot else None
    code = rpc("eth_getCode", implementation or address, tag)
    return Fingerprint(implementation=implementation, code_hash=hashlib.sha256(bytes.fromhex(code[2:])).hexdigest())
