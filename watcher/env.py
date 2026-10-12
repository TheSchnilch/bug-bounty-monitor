import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: Path = REPO_ROOT / ".env") -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    return {**values, **os.environ}


def etherscan_key(env: dict[str, str]) -> str:
    key = env.get("WATCHER_ETHERSCAN_API_KEY") or env.get("ETHERSCAN_API_KEY")
    if not key:
        raise SystemExit("set WATCHER_ETHERSCAN_API_KEY in .env or the environment")
    return key
