You are building the dependency map of the bug bounty program <program> on the chain <chain> (chain ID <chain_id>, Alchemy network <network>). You use your alchemy and etherscan mcps to look up live values onchain.

These are the contracts in scope. The verified source code of every contract is cloned once per unique code into the folder given next to it, so contracts with identical code share one folder:

<contracts>

For every contract find the other contracts it depends on and record them with the tools of the watcher MCP server. Register every contract with add_contract before you use it as a dependency target:
1. A dependency stored in a storage variable: a single dependency with the storage slot that holds the address and the address it points to now.
2. A dependency stored in an immutable variable or a constant: a single dependency with the address only, without a slot.
3. A dependency held in a mapping or an array, for example all markets of a manager: a list dependency with all addresses it holds now and the events that add new ones.

### Rules
- Only configurated addresses that the protocol interacts with are relevant. For example all addresses that have done a transfer with a token are not interesting. Only contracts the code really calls and interacts with.
- Contracts that can call restricted functions of a contract, for example through a role or a permission list, are also dependencies of it.
- Only go one level deep: record the dependencies of the contracts in scope, not the dependencies of the contracts you add. The one exception are proxies: if a contract in scope or a contract you add is a proxy, record its implementation as a single dependency of the proxy with the implementation slot, for example the EIP-1967 slot.
- If a contract already exists do not add it again but still create the dependency connections.
- Contracts that are added somewhere but are not actually used or not actually have some trusted permissions on the protocol should be ignored not be added as dependencies.

For every dependency without a storage slot also record every event that changes it with add_dependency_event, both the events that add addresses and the events that remove them. Each event gets a small Python script that returns the added and removed addresses, as described in the tool. Give every dependency a short name. Contracts with identical code can still have different dependencies and storage, so look at every address on its own.
