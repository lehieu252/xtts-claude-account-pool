# claude-account-manager (`acct`)

A Claude Code plugin for people with more than one Claude account (claude.ai login). Save your accounts, see the 5-hour / 7-day usage of all of them in one table, and switch between them **without logging out and back in**. Works in the CLI and the VS Code extension, on macOS, Windows and Linux.

| Command | What it does |
|---|---|
| `/acct:login <email>` or `/acct:login <name> [email]` | Log in to any account (new, or again) and save it. With just an email, the email is the account name, and the script checks that you signed in with that email in the browser. Opens a terminal window to finish the login; **your current account is not changed** |
| `/acct:save <name>` | Save the account you are **currently logged in** with, no new login needed |
| `/acct:list [--refresh]` | Table of saved accounts: email, organization, plan, 5-hour / 7-day usage with reset times. Accounts that need a new login are flagged. `▶` = current account |
| `/acct:switch <name>` | Switch to another saved account. Accepts the name, the email, or the part before `@` (e.g. `alice`) if it is unique |
| `/acct:logout <name>` | Remove an account from the list (deletes its saved token) |

## Install

In Claude Code:

```
/plugin marketplace add <github-owner>/claude-account-manager
/plugin install acct@claude-account-manager
```

In the VS Code extension, `/plugin` is not available. Type `/plugins` to open the plugin manager, or run the same two commands in a terminal with `claude plugin marketplace add …` and `claude plugin install … --scope user`.

Requires Python 3.8+ (`python3`, `python` or `py -3`). On Windows, Claude Code runs commands through Git Bash.

## First use

1. `/acct:save work`: save the account you are using now.
2. `/acct:login alice@example.com`: a terminal window and a browser login page open. Sign in with that account; if the page shows a code, paste it into the terminal.
3. `/acct:list`: see both accounts and their usage.

From then on, just run `/acct:switch work` or `/acct:switch alice`. Open tabs usually pick up the new account within 30-60 seconds. To be sure, open a new Claude tab and use `/resume` to continue your conversation.

If `/acct:list` shows **🔴 Log in again** for an account, run the `/acct:login <name>` command it shows. Nothing else in the table needs your attention.

**Do not run `/logout` before switching.** It can revoke the token of the account you leave, and the saved copy stops working.

## How it works

- Claude Code keeps its login in the **Keychain** on macOS (item `Claude Code-credentials`) or in `~/.claude/.credentials.json` on Windows / Linux. The account email and organization are in `~/.claude.json`, key `oauthAccount`.
- `login` runs `claude auth login` with `CLAUDE_CONFIG_DIR` pointing at a temporary directory, so your current login is untouched. When it finishes, the token is moved into the store and the temporary directory is deleted. On macOS the current login is backed up first and restored automatically if the Keychain turns out not to be keyed by directory.
- `save` copies the current token into a private store: Keychain item `claude-acct` (macOS) or `~/.claude-acct/secrets/<name>.json` with mode 600 (Windows / Linux). The list and usage cache are in `~/.claude-acct/accounts.json`, which holds no tokens.
- `switch` first saves the **latest** token of the current account (Claude Code refreshes tokens over time), then writes the target account's token and `oauthAccount` where Claude Code reads them.
- `switch` refuses when the current account is not saved, so you cannot lose that login by accident (`--force` skips the check). It also refuses to switch to an account that needs a new login.
- Tokens are **never** printed into the conversation. Each command runs the script directly (`disable-model-invocation`); Claude only shows the output.

## When an account needs a new login

An account is flagged **🔴 Log in again** when its refresh token has expired (`refreshTokenExpiresAt`) or the usage API rejects a still-valid token with HTTP 401 (the token was revoked). An expired short-lived access token is **not** flagged: Claude Code refreshes it automatically the next time you switch to that account.

## Limitations

- **Usage comes from an unofficial API** (`api.anthropic.com/api/oauth/usage`, the same data as `/usage`). Anthropic can change it at any time. Results are cached for 2 minutes to avoid HTTP 429.
- For an account you have not used for a while, the access token is expired, so `list` shows the last known usage with its age. The script does not refresh tokens itself, to avoid breaking the login. Switching to the account once brings fresh numbers.
- The claude.ai login can ask you to paste a code, and the chat box cannot take input, so `/acct:login` opens a separate terminal window (macOS: Terminal; Windows: a Git Bash window; Linux: the default terminal). If none can be opened, it prints the command to run yourself.
- In the VS Code extension, plugin commands may not appear in the `/` suggestion menu (known Claude Code issue). Typing the full command, e.g. `/acct:list`, works.
- API key, Bedrock and Vertex logins are not supported.
- If you set `CLAUDE_CONFIG_DIR` on macOS, the Keychain item name gets a suffix based on the path. The script follows community observations; this is not documented by Anthropic.
- Using several subscription accounts is subject to Anthropic's terms.

## Use from a terminal

```bash
bash scripts/acct.sh list
bash scripts/acct.sh switch work
```

Environment variable: `CLAUDE_ACCT_HOME` (store directory, default `~/.claude-acct`).

## Tests

```bash
bash tests/e2e.sh "$(mktemp -d)/t" scripts/claude_acct.py
```

Runs the full flow with fake accounts in a temporary directory, for both the Keychain and the file backend. Your real login is not touched.
