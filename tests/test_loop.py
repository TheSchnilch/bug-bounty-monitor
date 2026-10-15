import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from watcher.main import Loop


class LoopTest(unittest.TestCase):
    def test_failures_are_reported_once_per_error_and_recovery_is_reported(self) -> None:
        env = {"WATCHER_ETHERSCAN_API_KEY": "secret-etherscan-key", "WATCHER_DISCORD_WEBHOOK": "http://hook"}
        outcomes: list[Exception | None] = [
            RuntimeError("Etherscan getLogs failed for key secret-etherscan-key"), RuntimeError("Etherscan getLogs failed for key secret-etherscan-key"),
            KeyError("result"), None, None,
        ]
        sent: list[dict[str, Any]] = []

        def cycle(data_root: Path, cycle_env: dict[str, str]) -> None:
            outcome = outcomes.pop(0)
            if outcome:
                raise outcome

        loop = Loop(Path("/data/curvance"), env)
        with mock.patch("watcher.main.cycle", side_effect=cycle), mock.patch("watcher.main.say") as say, \
             mock.patch("watcher.main.send", side_effect=lambda send_env, message, files=(): sent.append(message)):
            for _ in range(5):
                loop.step()

        self.assertEqual([message["title"] for message in sent], ["curvance: cycle failed", "curvance: cycle failed", "curvance: cycles work again"])
        self.assertTrue(sent[0]["description"].startswith("RuntimeError: Etherscan getLogs failed for key <WATCHER_ETHERSCAN_API_KEY>\n```\nTraceback"))
        self.assertEqual(sent[0]["fields"][0]["value"], "1")
        self.assertTrue(sent[1]["description"].startswith("KeyError: 'result'"))
        self.assertEqual(sent[1]["fields"][0]["value"], "3")
        self.assertEqual(sent[2]["description"], "after 3 failed cycles")
        self.assertNotIn("secret-etherscan-key", str(sent) + str(say.call_args_list))


if __name__ == "__main__":
    unittest.main()
