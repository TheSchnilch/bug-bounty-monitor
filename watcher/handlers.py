import json
import os
import subprocess
import sys
from typing import Any, cast

from eth_abi.abi import decode as abi_decode

from .codes import canonical_type
from .db import ADDRESS_PATTERN, address_of

SCRIPT_TIMEOUT = 30
RUNNER = """
import json, sys
request = json.load(sys.stdin)
namespace = {}
exec(request["script"], namespace)
added, removed = namespace["handle"](request["event"])
json.dump([list(added), list(removed)], sys.stdout)
"""


def jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in cast(list[Any], value)]
    if isinstance(value, str) and ADDRESS_PATTERN.match(value):
        return value.lower()
    return value


def is_hashed(kind: str) -> bool:
    return kind in ("string", "bytes") or kind.startswith("(") or kind.endswith("]")


def decode(inputs: list[dict[str, Any]], log: dict[str, Any]) -> dict[str, Any]:
    names = [item["name"] or f"arg{index}" for index, item in enumerate(inputs)]
    unindexed = [index for index, item in enumerate(inputs) if not item.get("indexed")]
    values = abi_decode([canonical_type(inputs[index]) for index in unindexed], bytes.fromhex(log["data"][2:]))
    args = {names[index]: jsonable(value) for index, value in zip(unindexed, values)}
    topics = iter(log["topics"][1:])
    for index, item in enumerate(inputs):
        if item.get("indexed"):
            kind, word = canonical_type(item), next(topics)
            args[names[index]] = word if is_hashed(kind) else jsonable(abi_decode([kind], bytes.fromhex(word[2:]))[0])
    return {name: args[name] for name in names}


def run_script(script: str, event: dict[str, Any]) -> tuple[list[str], list[str]]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("WATCHER_")}
    try:
        process = subprocess.run([sys.executable, "-I", "-c", RUNNER], input=json.dumps({"script": script, "event": event}),
                                 capture_output=True, text=True, timeout=SCRIPT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {SCRIPT_TIMEOUT}s") from None
    if process.returncode:
        raise RuntimeError((process.stderr.strip().splitlines() or [f"exit {process.returncode}"])[-1])
    try:
        added, removed = json.loads(process.stdout)
        return [address_of(address) for address in added], [address_of(address) for address in removed]
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"invalid result {process.stdout.strip()[:200]}: {exc}") from None
