---
name: switch
description: Switch to another saved Claude account without logging out and back in; the current chat continues with the new account.
argument-hint: "<name | email | part before @> [--no-wait]"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" switch $ARGUMENTS 2>&1 || true`

Show the output above to the user VERBATIM (keep the markdown table). Add no commentary and run no other commands.
