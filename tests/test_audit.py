import unittest
from pathlib import Path

from watcher.audit import task


class AuditTest(unittest.TestCase):
    def test_first_audits_cover_everything_and_change_audits_only_the_changes(self) -> None:
        first = task(True, ["1. ignored"], Path("/data/codes"))
        self.assertTrue(first.startswith("This is the first audit of this program. Everything in /data/codes is in scope."))
        self.assertIn("Spawn 5 subagents", first)
        self.assertNotIn("ignored", first)
        change = task(False, ["1. TokenListed(cToken=0x1) was emitted by Market (0x2) in block 5 (transaction 0x3).", "   - effect"], Path("/data/codes"))
        self.assertIn("Your scope is these changes and everything they impact:\n\n1. TokenListed(cToken=0x1) was emitted by Market (0x2) "
                      "in block 5 (transaction 0x3).\n   - effect\n\nFor every change", change)
        self.assertNotIn("Spawn 5 subagents", change)
        self.assertNotIn("<", change)


if __name__ == "__main__":
    unittest.main()
