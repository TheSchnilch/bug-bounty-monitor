import json
import re
import sys
import urllib.request
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BLUE, GREEN, YELLOW, ORANGE, RED = 0x3498DB, 0x2ECC71, 0xF1C40F, 0xE67E22, 0xE74C3C
EXPLORERS = {"ethereum": "https://etherscan.io", "monad": "https://monadscan.com"}
SEVERITY_COLORS = {"critical": RED, "high": RED, "medium": ORANGE, "low": YELLOW}
SEVERITIES = ("critical", "high", "medium", "low", "informational")
ADDRESS = re.compile(r"(?<![0-9a-fA-Fx/])0x[0-9a-f]{40}(?![0-9a-f])")
TRANSACTION = re.compile(r"transaction (0x[0-9a-f]{64})")
MAX_FILES = 10


def linked(text: str, chain: str) -> str:
    explorer = EXPLORERS[chain]
    text = TRANSACTION.sub(lambda match: f"transaction [{match.group(1)[:10]}…]({explorer}/tx/{match.group(1)})", text)
    return ADDRESS.sub(lambda match: f"[{match.group(0)[:6]}…{match.group(0)[-4:]}]({explorer}/address/{match.group(0)})", text)


def embed(title: str, description: str, color: int, fields: Sequence[tuple[str, str]] = (), footer: str = "") -> dict[str, Any]:
    return {
        "title": title[:256],
        "description": description if len(description) <= 4096 else description[:4090] + "\n…",
        "color": color,
        "fields": [{"name": name[:256], "value": value[:1024], "inline": True} for name, value in fields],
        "footer": {"text": f"watch-code{' · ' + footer if footer else ''}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def changes_embed(program: str, chain: str, block: int, count: int, lines: list[str], diffs: int) -> dict[str, Any]:
    description = linked("\n".join(lines), chain) + (f"\n\n{diffs} code diff{'s' if diffs != 1 else ''} attached" if diffs else "")
    return embed(f"{program}: {count} dependency change{'s' if count != 1 else ''}", description, ORANGE, footer=f"block {block}")


def audit_started_embed(program: str, reason: str, block: int, model: str, effort: str, folder: Path) -> dict[str, Any]:
    return embed(f"{program}: audit started", reason[0].upper() + reason[1:], BLUE,
                 [("Block", str(block)), ("Model", f"{model}, effort {effort}")], footer=folder.name)


def audit_finished_embed(program: str, counts: Counter[str], minutes: int, cost: float, names: list[str], folder: Path) -> dict[str, Any]:
    total = sum(counts.values())
    color = next((SEVERITY_COLORS[severity] for severity in SEVERITIES if counts[severity] and severity in SEVERITY_COLORS), GREEN)
    fields = [(severity.capitalize(), str(counts[severity])) for severity in SEVERITIES if counts[severity]]
    fields += [(f"{severity} (unknown severity)", str(count)) for severity, count in sorted(counts.items()) if severity not in SEVERITIES]
    fields += [("Duration", f"{minutes} min"), ("Cost", f"${cost:.2f}")]
    description = "\n".join(f"• {name}" for name in names) or "No findings"
    return embed(f"{program}: audit finished, {total} finding{'s' if total != 1 else ''}", description, color, fields, footer=folder.name)


def redacted(text: str, env: dict[str, str]) -> str:
    for key, value in env.items():
        if key.startswith("WATCHER_") and len(value) >= 8:
            text = text.replace(value, f"<{key}>")
    return text


def cycle_failed_embed(program: str, error: str, trace: str, failures: int) -> dict[str, Any]:
    tail = trace.strip()[-1500:].replace("```", "'''")
    return embed(f"{program}: cycle failed", f"{error}\n```\n{tail}\n```", RED, [("Failed cycles in a row", str(failures))])


def cycle_recovered_embed(program: str, failures: int) -> dict[str, Any]:
    return embed(f"{program}: cycles work again", f"after {failures} failed cycle{'s' if failures != 1 else ''}", GREEN)


def send(env: dict[str, str], message: dict[str, Any], files: Sequence[Path] = ()) -> None:
    url = env.get("WATCHER_DISCORD_WEBHOOK")
    if not url:
        return
    payload = json.dumps({"embeds": [message]}).encode()
    if files:
        boundary = uuid.uuid4().hex
        parts = [(b'Content-Disposition: form-data; name="payload_json"\r\nContent-Type: application/json\r\n\r\n', payload)]
        parts += [(f'Content-Disposition: form-data; name="files[{index}]"; filename="{path.name}"\r\nContent-Type: text/plain\r\n\r\n'.encode(),
                   path.read_bytes()) for index, path in enumerate(files[:MAX_FILES])]
        body = b"".join(b"--" + boundary.encode() + b"\r\n" + head + content + b"\r\n" for head, content in parts) + b"--" + boundary.encode() + b"--\r\n"
        content_type = f"multipart/form-data; boundary={boundary}"
    else:
        body, content_type = payload, "application/json"
    request = urllib.request.Request(url, body, {"Content-Type": content_type, "User-Agent": "watch-code/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except OSError as exc:
        print(f"discord notification failed: {exc}", file=sys.stderr, flush=True)
