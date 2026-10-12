---
name: doctor
description: Check that the plugin can run (Python, Node.js, npm, Chrome, Claude Code login), install its Node dependency, and list the setup steps that are left.
disable-model-invocation: true
allowed-tools: Bash(bash *)
---
!`bash "${CLAUDE_PLUGIN_ROOT}/scripts/acct.sh" doctor 2>&1 || true`

Show the output above to the user VERBATIM (keep the markdown table). Add no commentary and run no other commands.
