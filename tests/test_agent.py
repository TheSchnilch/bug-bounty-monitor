import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from watcher.agent import CONTINUE, run_agent

FAKE_CLAUDE = """#!{python}
import json, os, sys
folder = os.path.dirname(sys.argv[0])
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.path.join(folder, "calls.jsonl"), "a") as calls:
    calls.write(json.dumps({{"args": args, "prompt": prompt, "cwd": os.getcwd(),
                             "env": sorted(k for k in os.environ if k.startswith("WATCHER_"))}}) + "\\n")
resume = "--resume" in args
session = args[args.index("--resume" if resume else "--session-id") + 1]
transcript = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "projects", "p", session + ".jsonl")
if resume and not os.path.exists(transcript) or os.path.exists(os.path.join(folder, "lost")):
    print("No conversation found with session ID: " + session, file=sys.stderr)
    sys.exit(1)
if not resume:
    os.makedirs(os.path.dirname(transcript), exist_ok=True)
    open(transcript, "w").close()
failures = os.path.join(folder, "failures")
left = int(open(failures).read()) if os.path.exists(failures) else 0
if left:
    open(failures, "w").write(str(left - 1))
    print(json.dumps({{"is_error": True, "result": "You've hit your usage limit"}}))
    sys.exit(1)
print(json.dumps({{"is_error": False, "result": "done", "session_id": session}}))
"""


class AgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.claude = self.root / "claude"
        self.claude.write_text(FAKE_CLAUDE.format(python=sys.executable))
        self.claude.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "config"), "WATCHER_ETHERSCAN_API_KEY": "secret"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_agent(self) -> dict[str, object]:
        with mock.patch("sys.stderr"):
            return run_agent("the prompt", self.root, self.root / "mcp.json", str(self.claude), backoff=(0,))

    def calls(self) -> list[dict[str, object]]:
        return [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]

    def test_runs_opus_with_the_prompt_and_without_watcher_secrets(self) -> None:
        result = self.run_agent()
        [call] = self.calls()
        self.assertEqual(call["args"], ["-p", "--model", "claude-opus-5-5", "--effort", "medium", "--mcp-config", str(self.root / "mcp.json"),
                                        "--dangerously-skip-permissions", "--output-format", "json", "--session-id", result["session_id"]])
        self.assertEqual((call["prompt"], call["cwd"], call["env"]), ("the prompt", str(self.root), []))

    def test_a_stopped_run_is_resumed_until_it_finishes(self) -> None:
        (self.root / "failures").write_text("2")
        result = self.run_agent()
        calls = self.calls()
        self.assertEqual([call["prompt"] for call in calls], ["the prompt", CONTINUE, CONTINUE])
        self.assertEqual([call["args"][-2:] for call in calls], [["--session-id", result["session_id"]]] + [["--resume", result["session_id"]]] * 2)
        self.assertEqual(result["result"], "done")

    def test_a_lost_session_raises(self) -> None:
        (self.root / "lost").touch()
        with self.assertRaisesRegex(RuntimeError, "cannot be resumed"):
            self.run_agent()


if __name__ == "__main__":
    unittest.main()
