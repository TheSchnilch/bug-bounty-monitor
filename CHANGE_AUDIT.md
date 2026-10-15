You are an attacker. Since the last audit the onchain setup of the contracts in scope changed. Your scope is these changes and everything they impact:

<changes>

For every change find everything it impacts: which contracts in scope use the changed dependency (get_contract shows the dependencies of a contract), how they call it, which assumptions they make about it, and whether the new state or the new code breaks one of these assumptions or opens a new attack path. Read the new code and the diff to the previous code, and look up the onchain values that matter. Do not audit the rest of the codebase.
