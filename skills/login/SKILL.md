---
name: login
description: Log in to any Claude account (new or again) and save it. Opens a terminal window to finish the login; your current account is not changed.
argument-hint: "<email | name> [email]"
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" login $ARGUMENTS 2>&1 || true`

Show the output above to the user VERBATIM (keep the markdown table). Add no commentary and run no other commands.
