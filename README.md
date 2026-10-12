# Onchain Contract Watcher

Second version, built from scratch. The first version lives in `archive/` as reference only.

## Usage

```sh
python3 -m watcher --data-root data/curvance
```

Runs a cycle every 5 minutes: first it syncs `<data-root>/watcher.db` (SQLite) with
`<data-root>/scope.json`, then it checks the chain for changes. Needs `WATCHER_ETHERSCAN_API_KEY` and
`WATCHER_ALCHEMY_API_KEY` in `.env` and `pip install -r requirements.txt`. A failed cycle is printed
and retried in the next one.

### Sync

`scope.json` lists the contracts in scope:

```json
{
  "program": "curvance",
  "chain": "monad",
  "contracts": [{"name": "MarketManagerIsolated - WMON | AUSD MarketManager", "address": "0x05e7…1554"}]
}
```

Every listed contract is stored with a lowercase address, `in_scope = 1` and `origin = 'manual'`. For
each one the verified source is requested from the Etherscan V2 API (`chain` selects the chain ID,
`ethereum` → 1, `monad` → 143) and its code identity (SHA-256 of the source files and the contract
name) is stored in `codes`, so contracts with identical code share one row. Unverified contracts are
stored without a code. All requests happen before one transaction, so a failed request writes
nothing. Every cycle updates names and adds new contracts.

Contracts removed from `scope.json` are pruned in the same cycle: they get `in_scope = 0`, their
dependencies are deleted, every out-of-scope contract that no dependency points to any more is
deleted (a removed contract that another in-scope contract still uses stays as out-of-scope), and
codes without contracts are deleted with their folder and ABI. An empty `scope.json` is refused.

Then every code is cloned once into `<data-root>/codes/<ContractName>-<first 8 hash characters>/`,
using the source of one of its addresses. Existing folders are skipped, and a source that no longer
matches its hash is refused.

Last, if at least one contract in `scope.json` was not in scope before, the dependency agent runs
headless with the prompt rendered from `DEPENDENCY_PROMPT.md` (program, chain, chain ID, Alchemy
network and every in-scope contract with its code folder): `claude -p --model claude-opus-5-5
--effort medium` in `<data-root>`, with the watcher MCP server from `mcp.json` and full tool access
(`--dangerously-skip-permissions`). `WATCHER_*` variables are removed from its
environment. The run gets a fixed session ID. If Claude stops before finishing (usage limit, crash or
error result), the watcher waits 1, 5, 15 and then every 30 minutes and resumes that session with a
continue message until it finishes. When it finishes, the watcher prints its answer, the session ID (continue it with
`claude --resume <id>` from `<data-root>`) and the cost.

### Watch

After the sync, the watcher checks every block from the last checked one up to the chain head minus
12 confirmations. The first cycle only records the current block as the start, there is no backfill.

- Slot dependencies: the slot is read at the end block. When it holds a new address, that contract is
  registered (`origin = 'slot'`, source cloned) and replaces the target. A proxy implementation
  recorded with its implementation slot is covered by this.
- Event dependencies: Etherscan `getLogs` per owning contract and event (the Alchemy free tier only
  allows 10 blocks per `eth_getLogs`). The address field is read from the topics or the data, using the ABI. A new address is registered
  (`origin = 'event'`) and added as a target. Only additions are handled so far.

Every change is printed, and every cycle ends with a line like
`checked up to block 123: 5 contracts, 3 dependencies, 0 changes`. Contracts that are no longer
targeted are pruned in the next cycle.

### MCP server

The watcher writes `<data-root>/mcp.json` for the dependency agent. Passing it to Claude (`claude --mcp-config <data-root>/mcp.json`)
makes Claude start the watcher's MCP server (`python -m watcher.mcp_server --data-root …`) over stdio
for that run. The server takes the chain from `scope.json` and the keys from `.env`
(`WATCHER_ETHERSCAN_API_KEY`, `WATCHER_ALCHEMY_API_KEY`), so Claude never sees them.

| Tool | Does |
| --- | --- |
| `list_contracts` | Every contract with name, scope flag, origin and code folder |
| `get_contract` | One contract with its dependencies, their targets and events |
| `list_codes` | Every unique code with its folder and addresses |
| `add_contract` | Registers a contract (`origin = 'agent'`, `in_scope = 0`) after checking on-chain that code is deployed, and clones its verified source |
| `add_dependency` | Adds a `single` (exactly one target) or `list` dependency; targets must be registered first. A slot is only allowed for `single` and must hold the target on-chain now |
| `add_dependency_target` | Adds a registered contract to a `list` dependency |
| `add_dependency_event` | Adds an event of the contract that changes the dependency; the canonical signature and the `address` field are checked against the contract's ABI |
| `remove_dependency` | Deletes a dependency with its targets and events |

The ABI of every code from Etherscan is saved as `<data-root>/abis/<ContractName>-<first 8 hash characters>.json`.

## Database

| Table | Content |
| --- | --- |
| `codes` | One row per unique code: `source_hash` (SHA-256 of the verified source files and contract name), `contract_name` |
| `contracts` | Every contract once, keyed by lowercase address: `name`, `in_scope`, `origin` (`manual`, `agent`, `event`, `slot`), `code` (link to `codes`, empty while unknown or unverified) |
| `dependencies` | Owned by one contract: `name` (unique per contract), optional `slot`, `kind` (`single` or `list`) |
| `dependency_targets` | The contracts a dependency points to; targets must be registered contracts |
| `dependency_events` | Events that update a dependency: `signature` and the `field` holding the address |
| `state` | One row with the last checked `block` |

A dependency without `slot` is a fixed link (for example an immutable). Foreign keys are enforced,
and deleting a dependency removes its targets and events.

## Tests

```sh
python3 -m unittest discover tests -v
npx pyright
```
