Your job is to set up or update the smart contracts of a bug bounty program. Use etherscan MCP for cloning. Here are all the links of the smart contracts to set up or to update: <insert links to contracts here>
The folder where the contracts must be set up or updated is: <insert absolute destination folder here>
The folder can be empty or already contain contracts from earlier runs. Only the contracts listed above need to be set up or updated; leave everything else as it is.
If the source code of a contract is not verified on Etherscan, do not clone it. Instead add a row to the table in UNVERIFIED_CONTRACTS.md in that folder with the columns Chain, Address, Explorer and Checked (the current UTC timestamp), creating the file if it does not exist. If a contract already listed there is verified now, clone it like the others and remove its row.

1. You clone every smart contract in the list locally. Every contract gets its own folder named: <name-of-the-contract>-<address>. If the folder already exists, replace its code with the new code, otherwise create it.
2. Then you try to minimize it. All files that are exact duplicates of each other go into a shared folder. If there are the exact same smart contracts multiple times just keep one file named by the contract and list all addresses that are this contract there. Just the plain addresses and no other text. All of these text files go into a folder called "addresses".
3. Fix the imports and everything else that needs to be fixed and compile the project with forge build.
