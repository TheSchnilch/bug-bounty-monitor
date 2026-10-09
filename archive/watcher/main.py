import argparse
import logging
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from pydantic import ValidationError

from .etherscan import ETHERSCAN_URL, Etherscan, clone
from .models import ClaudeResult, Fingerprint, Scope, State
from .sources import Rpc, fingerprint, load_program

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-opus-5-5"
CONFIRMATIONS = 12
ALCHEMY_URL = "https://{network}.g.alchemy.com/v2/{key}"
AUDITED_TAG = "audited"
CONTINUE = "Your previous run was interrupted. Continue the task from where you stopped and finish it."
BACKOFF = (60, 300, 900, 1800)

log = logging.getLogger("watcher")


class Chain(NamedTuple):
    alchemy_network: str
    chain_id: int


CHAINS = {
    "ethereum": Chain("eth-mainnet", 1),
    "monad": Chain("monad-mainnet", 143),
}


@dataclass
class Config:
    data_root: Path
    program: str = "pareto"
    immunefi: str = "https://immunefi.com/public-api/bounties.json"
    rpc_url: str = ""
    claude_bin: str = "claude"
    interval: int = 300
    etherscan_key: str = ""
    etherscan_url: str = ETHERSCAN_URL

    @property
    def project(self) -> Path:
        return self.data_root / "project"

    @property
    def scope_path(self) -> Path:
        return self.data_root / "scope.json"

    @property
    def state_path(self) -> Path:
        return self.data_root / "state.json"

    @property
    def findings(self) -> Path:
        return self.data_root / "findings"




def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def git(cfg: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["git", "-c", "user.name=watcher", "-c", "user.email=watcher@localhost", *args]
    return subprocess.run(command, cwd=cfg.project, capture_output=True, text=True, check=check)


def transcript_exists(session: str) -> bool:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return any(home.glob(f"projects/*/{session}.jsonl"))


def run_claude(cfg: Config, prompt: str, cwd: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("WATCHER_")}
    session = str(uuid.uuid4())
    failures = 0
    while True:
        started = transcript_exists(session)
        command = [cfg.claude_bin, "-p", "--model", MODEL, "--output-format", "json", "--resume" if started else "--session-id", session]
        process = subprocess.run(command, input=CONTINUE if started else prompt, cwd=cwd, capture_output=True, text=True, env=env)
        try:
            result = ClaudeResult.model_validate_json(process.stdout)
        except ValidationError:
            result = None
        if process.returncode == 0 and result is not None and not result.is_error:
            log.info("claude session %s finished (cost %s): %s", session, result.total_cost_usd, result.result)
            return
        output = (process.stdout + process.stderr)[-2000:]
        if "No conversation found with session ID" in output:
            raise RuntimeError(f"claude session {session} cannot be resumed: {output}")
        delay = BACKOFF[min(failures, len(BACKOFF) - 1)]
        failures += 1
        log.warning("claude session %s stopped (exit %s): %s; continuing in %ds", session, process.returncode, output.strip(), delay)
        time.sleep(delay)


def audit_prompt(diff: str | None) -> str:
    prompt = (REPO_ROOT / "AUDIT_PROMPT.md").read_text()
    if diff is None:
        return prompt
    return prompt + (
        "\nThe contracts were just updated. This is the diff of the update. Focus on the changed code "
        f"but consider the effects on the whole codebase.\n\n```diff\n{diff.rstrip()}\n```\n"
    )


def init_project(cfg: Config) -> None:
    if not (cfg.project / ".git").exists():
        cfg.project.mkdir(parents=True, exist_ok=True)
        git(cfg, "init", "-b", "main")
        (cfg.project / ".gitignore").write_text("out/\ncache/\n")


def commit(cfg: Config, addresses: list[str]) -> None:
    git(cfg, "add", "-A")
    git(cfg, "commit", "--allow-empty", "-m", f"update {stamp()}: {len(addresses)} contracts", "-m", "\n".join(addresses))


def update(cfg: Config, scope: Scope, changed: list[str]) -> tuple[list[str], list[str]]:
    init_project(cfg)
    etherscan = Etherscan(cfg.etherscan_key, CHAINS[scope.chain].chain_id, cfg.etherscan_url)
    cloned, unverified = clone(etherscan, changed, cfg.project)
    if git(cfg, "status", "--porcelain").stdout.strip():
        commit(cfg, cloned)
    if unverified:
        log.warning("%d addresses without verified source, retried at the next check: %s", len(unverified), ", ".join(unverified))
    log.info("cloned %d contracts with Etherscan", len(cloned))
    return cloned, unverified


def save_state(cfg: Config, current: dict[str, Fingerprint], unverified: list[str]) -> None:
    missing = {address.lower() for address in unverified}
    fingerprints = {address: logic for address, logic in current.items() if address.lower() not in missing}
    cfg.state_path.write_text(State(fingerprints=fingerprints).model_dump_json(indent=2))


def audit(cfg: Config) -> None:
    audited = git(cfg, "rev-parse", "--verify", AUDITED_TAG, check=False).returncode == 0
    diff = git(cfg, "diff", AUDITED_TAG, "HEAD").stdout if audited else None
    if diff is not None and not diff.strip():
        log.info("update changed no files; skipping the audit")
        return
    run_id = stamp()
    (cfg.project / "audit" / "findings").mkdir(parents=True, exist_ok=True)
    log.info("audit %s %s", run_id, "of the diff since the last audit" if audited else "of the whole codebase")
    run_claude(cfg, audit_prompt(diff), cfg.project)
    reports = sorted((cfg.project / "audit" / "findings").glob("*.md"))
    if reports:
        (cfg.findings / run_id).mkdir(parents=True, exist_ok=True)
        for report in reports:
            shutil.copy2(report, cfg.findings / run_id / report.name)
    git(cfg, "checkout", "-b", f"audit-{run_id}-{git(cfg, 'rev-parse', '--short', 'HEAD').stdout.strip()}")
    git(cfg, "add", "-A")
    git(cfg, "commit", "--allow-empty", "-m", f"audit {run_id}: {len(reports)} findings")
    git(cfg, "checkout", "main")
    git(cfg, "tag", "-f", AUDITED_TAG, "main")
    log.info("audit %s finished with %d findings", run_id, len(reports))


def sync_scope(cfg: Config, force: bool = False) -> Scope:
    previous = cfg.scope_path.read_text() if cfg.scope_path.exists() else None
    existing = Scope.model_validate_json(previous) if previous else Scope(program=cfg.program, assets=[])
    if existing.source == "manual":
        return existing
    slots = {asset.url: asset.updateSlot for asset in existing.assets}
    assets = load_program(cfg.immunefi, cfg.program)
    for asset in assets:
        asset.updateSlot = slots.get(asset.url)
    added = {asset.address for asset in assets} - {asset.address for asset in existing.assets}
    removed = {asset.address for asset in existing.assets} - {asset.address for asset in assets}
    if not (added or removed or force):
        return existing
    cfg.data_root.mkdir(parents=True, exist_ok=True)
    scope = Scope(program=cfg.program, assets=assets)
    cfg.scope_path.write_text(scope.model_dump_json(indent=2))
    log.info("scope synced with Immunefi: %d added, %d removed addresses", len(added), len(removed))
    if not (added or force):
        return scope
    try:
        prompt = (REPO_ROOT / "SCOPE_PROMPT.md").read_text().replace("data/scope.json", str(cfg.scope_path.resolve()))
        run_claude(cfg, prompt, cfg.data_root)
        scope = Scope.model_validate_json(cfg.scope_path.read_text())
        if sorted(asset.address for asset in scope.assets) != sorted(asset.address for asset in assets):
            raise RuntimeError("claude changed the addresses in the scope file")
    except Exception:
        if previous is None:
            cfg.scope_path.unlink(missing_ok=True)
        else:
            cfg.scope_path.write_text(previous)
        raise
    log.info("updateSlot set for %d of %d addresses", sum(1 for asset in scope.assets if asset.updateSlot), len(scope.assets))
    return scope


def observe(cfg: Config, rpc: Rpc) -> tuple[Scope, dict[str, Fingerprint], list[str]]:
    scope = sync_scope(cfg)
    block = int(rpc("eth_blockNumber"), 16) - CONFIRMATIONS
    current = {asset.address: fingerprint(rpc, asset.address, asset.updateSlot, block) for asset in sorted(scope.assets, key=lambda a: a.address)}
    state = State.model_validate_json(cfg.state_path.read_text()) if cfg.state_path.exists() else State()
    changed = [address for address, logic in current.items()
               if address not in state.fingerprints or state.fingerprints[address].code_hash != logic.code_hash]
    watched = sum(1 for asset in scope.assets if asset.updateSlot)
    log.info("block %d: %d contracts in scope (%d with updateSlot), %d new or changed", block, len(current), watched, len(changed))
    return scope, current, changed


def never_audited(cfg: Config) -> bool:
    if not (cfg.project / ".git").exists() or git(cfg, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        return False
    return git(cfg, "rev-parse", "--verify", AUDITED_TAG, check=False).returncode != 0


def setup(cfg: Config, rpc: Rpc) -> tuple[list[str], list[str]]:
    scope, current, changed = observe(cfg, rpc)
    cloned, unverified = update(cfg, scope, changed)
    save_state(cfg, current, unverified)
    return cloned, unverified


def check(cfg: Config, rpc: Rpc) -> list[str]:
    scope, current, changed = observe(cfg, rpc)
    cloned, unverified = update(cfg, scope, changed) if changed else ([], [])
    if cloned or never_audited(cfg):
        audit(cfg)
    save_state(cfg, current, unverified)
    return changed


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and not key.startswith("#"):
                values[key.strip()] = value.strip().strip("\"'")
    return {**values, **os.environ}


def rpc_url(env: dict[str, str], scope_path: Path) -> str:
    chain = Scope.model_validate_json(scope_path.read_text()).chain if scope_path.exists() else "ethereum"
    key = env.get("WATCHER_ALCHEMY_API_KEY") or env.get("ALCHEMY_API_KEY")
    return env.get("WATCHER_RPC_URL") or (ALCHEMY_URL.format(network=CHAINS[chain].alchemy_network, key=key) if key else "")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="watcher", description="Watch an Immunefi program's contracts and audit their updates")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "setup", "init-scope"])
    parser.add_argument("--data-root", default=str(REPO_ROOT / "data"))
    parser.add_argument("--program", default="pareto", help="Immunefi program slug (ignored for a manual scope.json)")
    parser.add_argument("--immunefi", default=Config.immunefi, help="bounties.json URL or local file")
    parser.add_argument("--interval", type=int, default=Config.interval, help="seconds between checks")
    parser.add_argument("--once", action="store_true", help="run a single check and exit")
    args = parser.parse_args(argv)
    env = load_env(REPO_ROOT / ".env")
    data_root = Path(args.data_root).resolve()
    cfg = Config(data_root, args.program, args.immunefi, rpc_url(env, data_root / "scope.json"),
                 env.get("WATCHER_CLAUDE_BIN") or shutil.which("claude") or "claude", args.interval,
                 env.get("WATCHER_ETHERSCAN_API_KEY") or env.get("ETHERSCAN_API_KEY", ""))
    cfg.data_root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(cfg.data_root / "watcher.log"), logging.StreamHandler()])
    if args.command == "init-scope":
        scope = sync_scope(cfg, force=True)
        if scope.source == "manual":
            print(f"{cfg.scope_path} is a manual scope; edit it by hand")
            return
        print(f"{cfg.scope_path}: updateSlot set for {sum(1 for asset in scope.assets if asset.updateSlot)} of {len(scope.assets)} addresses")
        return
    if not cfg.rpc_url:
        raise SystemExit("set WATCHER_ALCHEMY_API_KEY (or WATCHER_RPC_URL) in .env or the environment")
    if not cfg.etherscan_key:
        raise SystemExit("set WATCHER_ETHERSCAN_API_KEY in .env or the environment")
    rpc = Rpc(cfg.rpc_url)
    if args.command == "setup":
        cloned, unverified = setup(cfg, rpc)
        print(f"cloned {len(cloned)} contracts into {cfg.project}; no audit was run")
        if unverified:
            print(f"{len(unverified)} addresses without verified source: {', '.join(unverified)}")
        return
    while True:
        started = time.monotonic()
        try:
            check(cfg, rpc)
        except Exception:
            log.exception("check failed; nothing was saved, retrying at the next interval")
        if args.once:
            return
        time.sleep(max(0.0, cfg.interval - (time.monotonic() - started)))
