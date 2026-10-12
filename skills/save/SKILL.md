---
name: save
description: Save the currently logged-in Claude account to the list.
argument-hint: "<name>"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" save $ARGUMENTS 2>&1 || true`

Show the output above to the user VERBATIM (keep the markdown table). Add no commentary and run no other commands.
