import json
import urllib.error
import urllib.request
from typing import Any

ALCHEMY_NETWORKS = {"ethereum": "eth-mainnet", "monad": "monad-mainnet"}


def alchemy_url(env: dict[str, str], chain: str) -> str:
    key = env.get("WATCHER_ALCHEMY_API_KEY")
    if not key:
        raise SystemExit("set WATCHER_ALCHEMY_API_KEY in .env or the environment")
    return f"https://{ALCHEMY_NETWORKS[chain]}.g.alchemy.com/v2/{key}"


class Rpc:
    def __init__(self, url: str) -> None:
        self.url = url

    def call(self, method: str, *params: object) -> Any:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}).encode()
        request = urllib.request.Request(self.url, body, {"Content-Type": "application/json", "User-Agent": "watch-code/0.2"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} failed: {exc} {exc.read()[:500].decode(errors='replace')}") from None
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"{method} failed: {exc}") from None
        if "error" in data:
            raise RuntimeError(f"{method} failed: {data['error']}")
        return data["result"]
