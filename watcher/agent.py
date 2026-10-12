import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

MODEL = "claude-opus-5-5"
EFFORT = "medium"
CONTINUE = "Your previous run was interrupted. Continue the task from where you stopped and finish it."
BACKOFF = (60, 300, 900, 1800)


def transcript_exists(session: str) -> bool:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return any(home.glob(f"projects/*/{session}.jsonl"))


def run_agent(prompt: str, data_root: Path, mcp_config: Path, claude: str = "claude", backoff: tuple[int, ...] = BACKOFF) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("WATCHER_")}
    session = str(uuid.uuid4())
    failures = 0
    while True:
        started = transcript_exists(session)
        command = [claude, "-p", "--model", MODEL, "--effort", EFFORT, "--mcp-config", str(mcp_config),
                   "--dangerously-skip-permissions", "--output-format", "json", "--resume" if started else "--session-id", session]
        process = subprocess.run(command, input=CONTINUE if started else prompt, capture_output=True, text=True, cwd=data_root, env=env)
        try:
            result: dict[str, Any] = json.loads(process.stdout)
        except ValueError:
            result = {}
        if process.returncode == 0 and result and not result.get("is_error"):
            return result
        output = (process.stdout + process.stderr).strip()[-2000:]
        if "No conversation found with session ID" in output:
            raise RuntimeError(f"claude session {session} cannot be resumed: {output}")
        delay = backoff[min(failures, len(backoff) - 1)]
        failures += 1
        print(f"claude session {session} stopped (exit {process.returncode}): {output}; continuing in {delay}s", file=sys.stderr, flush=True)
        time.sleep(delay)
