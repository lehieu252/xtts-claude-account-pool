---
name: auto
description: Join the XTGroup pool server, make machine 2 the seat mover, turn automatic account switching on or off, show the pool state, or open the admin browser.
argument-hint: "<join <key>|mover <key>|enable|disable|status|browser>"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" auto $ARGUMENTS 2>&1 || true`

Show the output above to the user verbatim. Do not run other commands.
