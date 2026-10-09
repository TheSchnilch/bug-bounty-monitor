You are an attacker. Your goal is to find security vulnerabilities in this codebase. Spawn 5 subagents that also search for bugs. Give all of them different focus areas. All should in general search for bugs but in specific parts of the codebase you assign to them, but they can read the whole codebase as context. You search for bugs in the whole system. No agent should stop after one bug. If you found one continue and hunt for the next one. At the end the divided focus areas should together cover all of the codebase. Overlapping is ok, leaving a part out is not.

## General rules

- Only do what your prompt tells you do to. NOT MORE! Never!
- If you find a bug. Do not build and test the fix. Only report the finding.
- Only write tests when you have a specific lead to investigate. Do not just randomly write tests.
- If there is a dependencies folder then the dependencies in there are not the scope but just context. You have to use this context when hunting bugs in the part of the protocol that uses the dependencies.

## Report

A report must contain the root cause, description and the concrete attack steps. Every report must go into a single file in audit/findings.

- Do not use bold, italic or em-dashes
- Code snippets must always have links to the local code. You are encouraged to use code snippets in the report
- For every finding higher than low severity you want to report a working POC must be created and tested. Foundry is installed but you must find it first
- Findings must be written into the report destination which is normally a folder where each report should be added as a markdown file
- Every finding goes into its own markdown file
- Keep the reports as short as possible and on the point

The contracts were just updated. This is the diff of the update. Focus on the changed code but consider the effects on the whole codebase.

```diff
diff --git a/src/IdleCreditVault-0xbba97a1ca6d4966870cbe9f032ae9c6e6546d2ee/IdleCreditVault.sol b/src/IdleCreditVault-0xbba97a1ca6d4966870cbe9f032ae9c6e6546d2ee/IdleCreditVault.sol
index 6ed907f..b191c39 100644
--- a/src/IdleCreditVault-0xbba97a1ca6d4966870cbe9f032ae9c6e6546d2ee/IdleCreditVault.sol
+++ b/src/IdleCreditVault-0xbba97a1ca6d4966870cbe9f032ae9c6e6546d2ee/IdleCreditVault.sol
@@ -1,2 +1,2 @@
-// placeholder for the verified source of 0xbba97a1ca6d4966870cbe9f032ae9c6e6546d2ee (v1)
+// placeholder for the verified source of 0xbba97a1ca6d4966870cbe9f032ae9c6e6546d2ee (v2)
 contract IdleCreditVault {}
diff --git a/src/TransparentUpgradeableProxy-0xc35d078092872ec1f2ae82bcd6f0b6b89f0850de/TransparentUpgradeableProxy.sol b/src/TransparentUpgradeableProxy-0xc35d078092872ec1f2ae82bcd6f0b6b89f0850de/TransparentUpgradeableProxy.sol
index 4600979..4f2df30 100644
--- a/src/TransparentUpgradeableProxy-0xc35d078092872ec1f2ae82bcd6f0b6b89f0850de/TransparentUpgradeableProxy.sol
+++ b/src/TransparentUpgradeableProxy-0xc35d078092872ec1f2ae82bcd6f0b6b89f0850de/TransparentUpgradeableProxy.sol
@@ -1,2 +1,2 @@
-// placeholder for the verified source of 0xc35d078092872ec1f2ae82bcd6f0b6b89f0850de (v1)
+// placeholder for the verified source of 0xc35d078092872ec1f2ae82bcd6f0b6b89f0850de (v2)
 contract TransparentUpgradeableProxy {}
```
