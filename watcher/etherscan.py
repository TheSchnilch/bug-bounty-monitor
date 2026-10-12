import hashlib
import itertools
import json
import time
import urllib.parse
import urllib.request
from pathlib import PurePosixPath
from typing import Any, NamedTuple

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
LOG_PAGE = 1000


class Code(NamedTuple):
    source_hash: str
    contract_name: str


class Etherscan:
    def __init__(self, key: str, chain_id: int, url: str = ETHERSCAN_URL, min_interval: float = 0.25, attempts: int = 3) -> None:
        self.key, self.chain_id, self.url, self.min_interval, self.attempts = key, chain_id, url, min_interval, attempts
        self.last = 0.0
        self.cache: dict[str, dict[str, Any]] = {}

    def source(self, address: str) -> dict[str, Any]:
        if address not in self.cache:
            self.cache[address] = self.fetch(address)
        return self.cache[address]

    def request(self, params: dict[str, object]) -> Any:
        query = urllib.parse.urlencode({"chainid": self.chain_id, **params, "apikey": self.key})
        error: object = None
        for attempt in range(self.attempts):
            time.sleep(max(2.0 * attempt, self.last + self.min_interval - time.monotonic()))
            self.last = time.monotonic()
            try:
                request = urllib.request.Request(f"{self.url}?{query}", headers={"User-Agent": "watch-code/0.2"})
                with urllib.request.urlopen(request, timeout=60) as response:
                    data = json.loads(response.read())
                if data.get("status") == "1" and data.get("result"):
                    return data["result"]
                if data.get("message") == "No records found":
                    return []
                error = data.get("result") or data.get("message")
            except (OSError, ValueError) as exc:
                error = exc
        raise RuntimeError(f"Etherscan {params['action']} for {params['address']} failed: {error}")

    def fetch(self, address: str) -> dict[str, Any]:
        return self.request({"module": "contract", "action": "getsourcecode", "address": address})[0]

    def logs(self, address: str, topic0: str, start: int, end: int) -> list[dict[str, Any]]:
        logs: list[dict[str, Any]] = []
        for page in itertools.count(1):
            batch = self.request({"module": "logs", "action": "getLogs", "address": address, "topic0": topic0,
                                  "fromBlock": start, "toBlock": end, "page": page, "offset": LOG_PAGE})
            logs += batch
            if len(batch) < LOG_PAGE:
                return logs
        return logs

    def code(self, address: str) -> Code | None:
        return code_of(self.source(address))


def code_of(entry: dict[str, Any]) -> Code | None:
    if not str(entry.get("SourceCode") or "").strip():
        return None
    name = str(entry.get("ContractName") or "")
    content = json.dumps({"contract_name": name, "files": source_files(entry)}, sort_keys=True, separators=(",", ":"))
    return Code(hashlib.sha256(content.encode()).hexdigest(), name)


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
    return {"/".join(part for part in PurePosixPath(path).parts if part not in ("/", ".", "..")): item["content"] for path, item in sources.items()}
