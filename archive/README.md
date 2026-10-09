# Onchain Contract Watcher

Watches the contracts of an Immunefi bug bounty program and audits every update with Claude Code.

Every 5 minutes the watcher:

1. Loads the program from Immunefi's public `bounties.json` and syncs `scope.json` with its
   contract addresses, keeping every `updateSlot` already set. When addresses were added (or the
   file is new), runs Claude Code with `SCOPE_PROMPT.md`, which uses the Etherscan MCP to find the
   implementation slot of every proxy and writes it to `updateSlot`. If that run fails or breaks
   the file, the previous `scope.json` is restored and the next check tries again.
2. Fingerprints every asset in `scope.json` 12 blocks behind the chain head over JSON-RPC. With an
   `updateSlot`, it reads that slot to get the implementation address and hashes the
   implementation's bytecode; without one, it hashes the asset's own bytecode.
3. For every asset that is new or whose hash changed (a slot change to identical bytecode does not
   count), fetches the verified source from the Etherscan V2 API into
   `project/<ContractName>-<address>/` and commits it in the project's git repo. Addresses without
   verified source are skipped, logged and retried at the next check. Contracts with the same name
   and identical source are stored once: as `project/<ContractName>/` (or
   `project/<ContractName>-<first address>/` when the name has several versions), with all their
   addresses listed one per line in `project/addresses/<folder>.txt`. Only the listed addresses
   are cloned, not proxy implementations, so a proxy upgrade alone produces no diff yet.
4. Runs Claude Code with `AUDIT_PROMPT.md`. The first audit covers the whole codebase; later
   audits also get the `git diff` since the last audit.
5. Copies the reports from `audit/findings` to `findings/<run-id>/`, commits everything the
   audit wrote (PoCs etc.) to a branch `audit-<run-id>-<commit>`, switches back to `main`, and
   moves the `audited` tag to it.

Claude is only used for the scope prompt and the audit. Every Claude call gets its own session ID. If Claude stops before finishing, for example at the
usage limit, the watcher waits (1, 5, 15, then every 30 minutes) and resumes the same session with
`--resume` and a short continue message until it finishes. The fingerprints are saved only after
the audit finished, so a failed RPC call is simply retried on the next check.

## Setup

```sh
pip install -r requirements.txt
# .env: WATCHER_ALCHEMY_API_KEY=… and WATCHER_ETHERSCAN_API_KEY=…
python3 -m watcher init-scope   # sync data/scope.json and let Claude fill in every updateSlot
python3 -m watcher              # check every 5 minutes
python3 -m watcher --once       # single check
```

`init-scope` always runs the scope prompt; the loop runs it only for new addresses. You can also
edit `updateSlot` by hand, for example to the EIP-1967 slot
`0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc`. Assets without a slot are not
watched.

### Setup without an audit

```sh
python3 -m watcher setup --data-root data/curvance
```

Does steps 1 to 3 once (fingerprint, clone, deduplicate, commit) and exits without an audit. Running
it again with nothing new still deduplicates an existing project. The next
`python3 -m watcher` run audits the whole codebase once, then watches for updates.

### Programs outside Immunefi

Give every program its own data root. For a scope you maintain by hand, write `scope.json`
yourself with `"source": "manual"`; the watcher then never syncs it with Immunefi and never runs the
scope prompt. `chain` selects the Alchemy network and the Etherscan chain ID (`ethereum` → 1,
`monad` → 143); it defaults to `ethereum`.

```json
{
  "program": "curvance",
  "source": "manual",
  "chain": "monad",
  "assets": [{"name": "Market Manager", "address": "0x…", "updateSlot": "0x3608…2bbc"}]
}
```

```sh
python3 -m watcher --data-root data/curvance
```

Options: `--data-root` (default `./data`), `--program` (default `pareto`), `--immunefi`
(bounties.json URL or local file), `--interval` (seconds, default 300).
`WATCHER_RPC_URL` replaces the Alchemy URL, `WATCHER_CLAUDE_BIN` the `claude` binary.
`WATCHER_*` variables are not passed to Claude.

## Data

```text
data/
  scope.json          program contract addresses and their updateSlot (filled by Claude)
  state.json          last implementation and code hash of every watched contract
  watcher.log
  project/            git repo with the cloned contracts (main, audit-* branches, audited tag)
    addresses/        one file per deduplicated contract listing all its addresses
  findings/<run-id>/  audit reports
```

## Tests

```sh
python3 -m unittest tests.test_pipeline -v
npx pyright
```
