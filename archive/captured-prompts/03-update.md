Your job is to set up or a bug bounty program. Use etherscan MCP for cloning. Here are all the links of the smart contrats to setup or to update: https://etherscan.io/address/0xbba97a1ca6d4966870cbe9f032ae9c6e6546d2ee, https://etherscan.io/address/0xc35d078092872ec1f2ae82bcd6f0b6b89f0850de
The folder where the contracts must be set up or updated is: /tmp/claude-1000/-home-schnilch-AI-watch-code/eba85673-de77-430b-8bc0-a709d18d4574/scratchpad/prompts-v2/data/project
If the source code of a contract is not verified on Etherscan, do not clone it. Instead add a row to the table in UNVERIFIED_CONTRACTS.md in that folder with the columns Chain, Address, Explorer and Checked (the current UTC timestamp), creating the file if it does not exist. If a contract already listed there is verified now, clone it like the others and remove its row.

### Update
1. The baseline is already set up. Only the contracts listed above need an update. Get the new code and clone them locally. In their folder named: <name-of-the-contract>-<address>. If a contract is new and has not existed before create the folder.
2. Then you try to minimize it. All files that are exact duplicates of each other go into a shared folder. If there are the exact same smart contracts multiple times just keep one file in the contract folder with an import of the contract from the reference folder.
3. Fix the imports and everything else that needs to be fixed and compile the project with forge build.
