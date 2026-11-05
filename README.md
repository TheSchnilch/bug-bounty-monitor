# watch-code

Watches the smart contracts of a bug bounty program and everything they depend on, and has Claude
audit every onchain change as soon as it happens.

> **Status: prototype.** It has only been run end to end on one program, the Curvance bug bounty on
> Monad (`data/curvance/scope.json`). Expect rough edges on any other scope.

## The idea

The end goal is a tool where you only define a scope, the contracts of a bug bounty program, and
everything else happens automatically: it finds out which other contracts the scope depends on,
keeps that map current while the protocol changes onchain, and audits each change the moment it
happens.

This repository is a working prototype of that loop. The scope is still a hand-written file, only
two chains are configured, and some parts are deliberately simple (see [Limitations](#limitations)).

## How it works

Every 5 minutes the watcher runs one cycle:

1. **Sync the scope.** Reads `scope.json`, fetches the verified source of every new contract from
   Etherscan and clones it. Contracts with identical source share one folder. Contracts removed
   from the scope are pruned together with everything only they depended on.
2. **Map the dependencies.** When contracts are new in scope, a Claude Code agent reads their code
   and records what each one depends on, through an MCP server the watcher provides: addresses in
   storage slots (proxy implementations included), immutables, and lists such as all markets of a
   manager. For every event that changes such a list, the agent writes a small Python script that
   turns the event into added and removed addresses.
3. **Watch the chain.** Reads every recorded slot, fetches the logs of every recorded event, runs
   the scripts and updates the map. New contracts are registered and cloned, removed ones pruned.
4. **Audit.** The first audit covers the whole scope. After that, every cycle with changes starts
   an audit scoped to exactly those changes and everything they affect. It gets the onchain cause,
   the effect on the dependency map, and a diff of replaced code that ignores comments and
   whitespace:

   ```
   1. PermissionsUpdated(permissionsType=0x8ecd…, addressUpdated=0x3b08…, isAdded=true) was emitted
      by CentralRegistry (0x1310…) in block 111657782 (transaction 0xf620…).
      - Market managers of CentralRegistry (0x1310…) now also contains MarketManagerIsolated (0x3b08…).
        Its code: …/codes/MarketManagerIsolated-a57778d6
   2. Storage slot 0x8 of savUSD | USDC cUSDC (0x9891…) changed, seen in block 113660575.
      - Interest rate model of savUSD | USDC cUSDC (0x9891…) now points to … instead of ….
        What changed in the code, without comments and whitespace: …/diff-1.diff
   ```

5. **Notify.** Posts dependency changes (with the diffs attached), audit start and end (with
   findings per severity) and failed cycles to a Discord webhook.

## Requirements

- Python 3.11 or newer
- [Claude Code](https://docs.claude.com/en/docs/claude-code), installed and logged in. The agents run
  as `claude -p` and use your Claude usage.
- The `alchemy` and `etherscan` MCP servers configured in Claude Code (`claude mcp list`). The agents
  use them for onchain lookups.
- An [Etherscan API key](https://etherscan.io/apis) (V2). The free plan covers Ethereum and Monad, but
  not BSC, Optimism or Base.
- An [Alchemy](https://www.alchemy.com) API key with the scope's network enabled.
- Optional: a Discord webhook for notifications, and [Foundry](https://getfoundry.sh) if audits should
  build and run proofs of concept.

## Setup

```sh
git clone <this repository> watch-code
cd watch-code
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then fill in the keys
```

| Variable | Used for |
| --- | --- |
| `WATCHER_ETHERSCAN_API_KEY` | Verified source, ABIs and event logs |
| `WATCHER_ALCHEMY_API_KEY` | Storage slots, contract code and block numbers |
| `WATCHER_DISCORD_WEBHOOK` | Notifications (optional) |

## Running

```sh
.venv/bin/python -m watcher --data-root data/curvance
```

Run it from the repository folder. It runs until stopped; on a server, start it with `nohup` or in
`tmux`. Every cycle ends with a line like
`checked up to block 113660575: 232 contracts (133 in scope, 99 dependencies), 425 dependency links, 0 changes`.

The first cycle on a new scope does the expensive work: cloning, the dependency agent and the first
audit. After that only changes cost anything.

### Defining a scope

Create `data/<program>/scope.json`:

```json
{
  "program": "curvance",
  "chain": "monad",
  "contracts": [
    {"name": "MarketManagerIsolated - WMON | AUSD MarketManager", "address": "0x05e70717fA8BD0F21a9F826d093d99f6Da4f1554"}
  ]
}
```

`chain` is `ethereum` or `monad`. Adding a chain means adding it to `CHAIN_IDS`
([watcher/scope.py](watcher/scope.py)), `ALCHEMY_NETWORKS` ([watcher/rpc.py](watcher/rpc.py)) and
`EXPLORERS` ([watcher/notify.py](watcher/notify.py)). You can edit the file while the watcher runs;
the next cycle picks up added and removed contracts.

### Prompts

The agents' instructions are plain Markdown files in the repository root, so they can be tuned
without touching code:

| File | Used by |
| --- | --- |
| [DEPENDENCY_PROMPT.md](DEPENDENCY_PROMPT.md) | The dependency agent |
| [AUDIT_PROMPT.md](AUDIT_PROMPT.md) | Every audit: program, folders, rules and report format |
| [FIRST_AUDIT.md](FIRST_AUDIT.md) | The first audit of a program (whole scope, 5 subagents) |
| [CHANGE_AUDIT.md](CHANGE_AUDIT.md) | Audits of changes (only the changes and their impact) |

## Costs and safety

Read this before running it.

- **The agents have full access to the machine.** They run with `--dangerously-skip-permissions`, so
  they can run any command and read any file your user can, including `.env`. Run the watcher on a
  dedicated machine or user account.
- **Event scripts are executed.** The scripts the dependency agent writes run in a separate Python
  process with a 30 second timeout and without the `WATCHER_*` variables, but they are not sandboxed.
- **Audits cost real money.** Measured on Curvance: mapping the dependencies of 12 contracts took
  4 minutes and $1.71; the first audit of the full scope (133 contracts) took 46 minutes and $44 at
  `xhigh` effort. Every cycle with changes starts another audit, and there is no daily limit.
- **API usage.** For the Curvance scope a cycle makes about 100 Etherscan calls, roughly 30,000 a
  day; the free plan allows 100,000.

### Event scripts

Every recorded event has a script with a function `handle(event)` that returns `(added, removed)`,
two lists of addresses:

```python
def handle(event):
    manager = event["args"]["positionManager"]
    return ([manager], []) if event["args"]["addPerms"] else ([], [manager])
```

`event` has `address`, `signature`, `block`, `transaction`, `topics`, `data` and `args`, the
parameters decoded with the ABI by name (unnamed ones as `arg0`, `arg1`, …; addresses lowercase,
bytes as hex, tuples and arrays as lists, hashed indexed parameters as the raw topic). Logs are
processed in chain order and, within one log, removals before additions, so an event that replaces
one address with another works. For a `single` dependency the added address replaces the target. A
failing script is reported and its log skipped.

### Watcher MCP server

The agents start it themselves from `mcp.json`. It reads the keys from `.env` itself, so they are not
part of the agents' prompts or environment. The audits get a read-only version.

| Tool | Does | Audit |
| --- | --- | --- |
| `list_contracts` | Every contract with name, scope flag, origin and code folder | yes |
| `get_contract` | One contract with its dependencies, their targets and events | yes |
| `list_codes` | Every unique code with its folder and addresses | yes |
| `compare_codes` | Diff of two codes without comments and whitespace; flattened sources are compared per original file | yes |
| `add_contract` | Registers a contract after checking onchain that code is deployed, and clones its verified source | |
| `add_dependency` | Adds a `single` or `list` dependency; targets must be registered, a slot must hold the target onchain | |
| `add_dependency_target` | Adds a registered contract to a `list` dependency | |
| `add_dependency_event` | Adds an event with its script; the signature is checked against the ABI | |
| `remove_dependency` | Deletes a dependency with its targets and events | |

### Database

| Table | Content |
| --- | --- |
| `codes` | One row per unique code: `source_hash`, `contract_name` |
| `contracts` | Every contract by lowercase address: `name`, `in_scope`, `origin` (`manual`, `agent`, `event`, `slot`), `code` |
| `dependencies` | Owned by one contract: `name`, optional `slot`, `kind` (`single` or `list`) |
| `dependency_targets` | The contracts a dependency points to |
| `dependency_events` | Events that update a dependency: `signature` and its `script` |
| `state` | The last checked block |

Pruning keeps every contract reachable from the scope through dependency targets, at any depth, and
deletes the rest with its code folders.

## License

[MIT](LICENSE)
