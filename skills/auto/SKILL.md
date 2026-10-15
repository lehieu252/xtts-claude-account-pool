---
name: auto
description: Configure the usage thresholds, enable or disable automatic Premium seat rotation, and inspect its status.
argument-hint: "<configure|enable|disable|status|check> [options]"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" auto $ARGUMENTS 2>&1 || true`

Show the output above to the user verbatim. Do not run other commands.
