import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from watcher.compare import code_lines, compare, split_files
from watcher.notify import embed, linked, send

FLAT = """// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

// contracts/libraries/SwapperLib.sol

/// @notice swaps
library SwapperLib {
    function swap() internal { check("http://x"); }
}

// contracts/market/Market.sol

contract Market {
    uint256 a; // note
}
"""


def folder(root: Path, files: dict[str, str]) -> Path:
    for path, content in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)
    return root


class CompareTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def test_flattened_sources_are_split_into_their_files(self) -> None:
        files = split_files(folder(self.root / "a", {"Market.sol": FLAT}))
        self.assertEqual(sorted(files), ["(header before the first file)", "contracts/libraries/SwapperLib.sol", "contracts/market/Market.sol"])
        self.assertEqual(code_lines(files["contracts/libraries/SwapperLib.sol"]),
                         ["library SwapperLib {", 'function swap() internal { check("http://x"); }', "}"])

    def test_only_code_changes_are_shown_per_file(self) -> None:
        old = folder(self.root / "Market-11111111", {"Market.sol": FLAT})
        changed = FLAT.replace("/// @notice swaps", "/// @notice swaps tokens").replace("uint256 a; // note", "uint256 b;")
        new = folder(self.root / "Market-22222222", {"MarketV2.sol": changed})
        text = compare(old, new)
        self.assertTrue(text.startswith("2 files are identical apart from comments and whitespace, 1 files differ\n"))
        self.assertIn("--- Market-11111111/contracts/market/Market.sol\n+++ Market-22222222/contracts/market/Market.sol", text)
        self.assertIn("-uint256 a;\n+uint256 b;", text)
        self.assertNotIn("SwapperLib", text)

    def test_files_on_one_side_are_listed(self) -> None:
        old = folder(self.root / "A-1", {"src/A.sol": "contract A {}", "src/B.sol": "contract B {}"})
        new = folder(self.root / "A-2", {"src/A.sol": "contract A {}", "src/C.sol": "contract C {}"})
        self.assertEqual(compare(old, new).splitlines()[:3], [
            "1 files are identical apart from comments and whitespace, 0 files differ", "only in A-1: src/B.sol", "only in A-2: src/C.sol",
        ])


class NotifyTest(unittest.TestCase):
    def test_embeds_and_files_are_posted_and_failures_survive(self) -> None:
        received: list[tuple[str, bytes]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                received.append((self.headers["Content-Type"], self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = {"WATCHER_DISCORD_WEBHOOK": f"http://127.0.0.1:{server.server_port}/hook"}
        diff = Path(tempfile.mkdtemp()) / "diff-1.diff"
        diff.write_text("-old\n+new\n")
        message = embed("title", "x" * 5000, 1, [("Block", "5")])
        send(env, message)
        send(env, message, [diff])
        send({}, message)
        send({"WATCHER_DISCORD_WEBHOOK": "http://127.0.0.1:1/hook"}, message)
        self.assertEqual(len(received), 2)
        content_type, body = received[0]
        self.assertEqual(content_type, "application/json")
        posted: dict[str, Any] = json.loads(body)["embeds"][0]
        self.assertEqual((posted["title"], len(posted["description"]), posted["fields"]), ("title", 4092, [{"name": "Block", "value": "5", "inline": True}]))
        content_type, body = received[1]
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="files[0]"; filename="diff-1.diff"', body)
        self.assertIn(b"-old\n+new\n", body)
        self.assertIn(b'name="payload_json"', body)

    def test_addresses_and_transactions_become_explorer_links(self) -> None:
        address, transaction, topic = "0x" + "ab" * 20, "0x" + "cd" * 32, "0x" + "ef" * 32
        text = linked(f"Event(type={topic}, who={address}) (transaction {transaction})", "monad")
        self.assertEqual(text, f"Event(type={topic}, who=[0xabab…abab](https://monadscan.com/address/{address})) "
                               f"(transaction [0xcdcdcdcd…](https://monadscan.com/tx/{transaction}))")

if __name__ == "__main__":
    unittest.main()
