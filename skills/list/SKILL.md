---
name: list
description: List saved Claude accounts with 5-hour / 7-day usage, and flag the ones that need a new login.
argument-hint: "[--refresh]"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" list $ARGUMENTS 2>&1 || true`

Show the output above to the user VERBATIM (keep the markdown table). Add no commentary and run no other commands.
