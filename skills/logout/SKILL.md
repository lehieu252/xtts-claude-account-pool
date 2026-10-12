---
name: logout
description: Remove a Claude account from the saved list.
argument-hint: "<name | email | part before @>"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" remove $ARGUMENTS 2>&1 || true`

Show the output above to the user VERBATIM (keep the markdown table). Add no commentary and run no other commands.
