# xtts-claude-account-pool (`acct`)

A Claude Code plugin for people with more than one Claude account (claude.ai login). Save your accounts, see the 5-hour / 7-day usage of all of them in one table, and switch between them **without logging out and back in**. Works in the CLI and the VS Code extension, on macOS, Windows and Linux.

| Command | What it does |
|---|---|
| `/acct:login <email>` or `/acct:login <name> [email]` | Log in to any account (new, or again) and save it. With just an email, the email is the account name, and the script checks that you signed in with that email in the browser. Opens a terminal window to finish the login; **your current account is not changed** |
| `/acct:save <name>` | Save the account you are **currently logged in** with, no new login needed |
| `/acct:list [--refresh]` | Table of saved accounts: email, organization, plan, 5-hour / 7-day usage with reset times. Accounts that need a new login are flagged. `▶` = current account |
| `/acct:switch <name> [--no-wait]` | Switch to another saved account and **keep working in the same chat**. Accepts the name, the email, or the part before `@` (e.g. `alice`) if it is unique |
| `/acct:auto <configure|enable|disable|status|check>` | Rotate the organization's Premium seat to the next saved account when the active Premium account reaches the configured usage threshold |
| `/acct:logout <name>` | Remove an account from the list (deletes its saved token) |

## Install

In Claude Code:

```
/plugin marketplace add lehieu252/xtts-claude-account-pool
/plugin install acct@xtts-claude-account-pool
```

In the VS Code extension, type `/plugins` to open the plugin manager. You can also run the commands from a terminal:

```bash
claude plugin marketplace add lehieu252/xtts-claude-account-pool
claude plugin install acct@xtts-claude-account-pool --scope user
```

Requirements: Claude Code, Python 3.8+, Node.js 20+, npm, and Google Chrome. On Windows, Claude Code runs the plugin's shell scripts through Git Bash.

The seat automation uses `playwright-core`. After installing the plugin, install its Node dependency in the installed plugin's cache directory (replace `0.7.0` with the installed plugin version if it differs):

```bash
cd ~/.claude/plugins/cache/xtts-claude-account-pool/acct/0.7.0
npm ci
```

The browser profile and saved sign-in session are local to each machine, under `~/.claude-acct/browser-profile` by default.

## Set up a new machine

1. Install and sign in to Claude Code. Install Google Chrome, Python 3.8+, and Node.js 20+.
2. Install the plugin using the marketplace commands above, then run `npm ci` in its installed cache directory.
3. Start Claude Code and save each account on this machine. The old machine's account tokens and Chrome session are not transferred:

   ```text
   /acct:login xtts xtts@example.com
   /acct:login info info@example.com
   /acct:login sales sales@example.com
   /acct:list --refresh
   ```

   Replace the example emails with the actual addresses. Run `/acct:login <name> <email>` for every pool account. Each one must be a member of the same organization.
4. Configure the current Premium account, rotation order, and threshold. Put the current Premium account first; the next name is the first rotation target:

   ```text
   /acct:auto configure --premium xtts --order xtts,info,sales
   ```

5. Open the dedicated browser profile once:

   ```text
   /acct:auto browser
   ```

   Sign in to Claude as an organization owner or admin and complete any Cloudflare check. This profile stores that login locally for future seat changes.
6. Check `/acct:list --refresh` and `/acct:auto status`. Confirm the current Premium account and order are correct and that the next account has usable capacity. Rotation is still off at this point.
7. Turn on automatic rotation:

   ```text
   /acct:auto enable
   ```

   To stop it later, run `/acct:auto disable`.

## First use

1. `/acct:save work`: save the account you are using now.
2. `/acct:login alice@example.com`: a terminal window and a browser login page open. Sign in with that account; if the page shows a code, paste it into the terminal.
3. `/acct:list`: see both accounts and their usage.

From then on, just run `/acct:switch work` or `/acct:switch alice`, wait for the confirmation, and keep working in the same chat. No new tab or `/resume` needed.

### Automatic Premium seat rotation

The rotation is opt-in. The default trigger is **90% for 5-hour usage and 90% for 7-day usage**. Configure the current Premium account and the order in which seats should rotate:

```
/acct:auto configure --premium xtts --order xtts,info,sales
/acct:auto browser
/acct:auto status
/acct:auto enable
```

To change thresholds for a test, set the 5-hour and 7-day values independently. For example, trigger at 75% for 5-hour usage while keeping weekly at 90%:

```
/acct:auto configure --premium xtts --order xtts,info,sales --daily-threshold 75 --weekly-threshold 90
```

Use `--daily-threshold 90 --weekly-threshold 75` to test the reverse, or `--threshold 75` as shorthand to set both to 75%. Daily means the 5-hour usage window shown by `/acct:list`; weekly means the 7-day window. Run `/acct:auto status` to see the active values. Re-running `configure` keeps rotation off until you enable it again.

The plugin checks the active Premium account before each prompt and tool call. When either the 5-hour or 7-day usage reaches the threshold, it changes the old member to Standard, promotes the next eligible member to Premium in the Members page, verifies the displayed tiers, and then switches Claude Code to that account. If Claude Code reports a usage-related `rate_limit` failure, a `StopFailure` hook makes one best-effort rotation attempt using the same fresh-usage and seat checks. It ignores ordinary API throttling and other errors. Use `/acct:auto disable` to stop rotation.

On interactive Claude Code **v2.1.234 or later**, Claude Code has its own automatic continue behavior after a claude.ai usage limit. Keep `autoContinueAtUsageLimit` enabled and leave the session open. The plugin can rotate credentials after a recognized usage failure, but Claude Code's `StopFailure` hook has no control to force an immediate retry. Native automatic continue may wait for the reported reset, and changing accounts during that wait can affect whether Claude Code continues; verify this behavior with your installed Claude Code version before relying on unattended recovery.

Run `/acct:auto browser` once to open the persistent automation profile in regular Chrome. Sign in as an organization owner/admin if needed. The plugin uses Chrome's local debugging endpoint to control this dedicated profile; your normal Chrome profile stays separate. Chrome 136 and later block remote debugging of the default user-data directory, so the plugin uses this dedicated profile. The Members page defaults to `https://claude.ai/admin-settings/members`; override it with `--members-url` if your organization uses a different path on claude.ai.

The account order is explicit and cyclic. Before choosing a destination, the plugin checks saved credentials and refreshes that account's usage (using the short cache to avoid excessive requests). It skips accounts at either configured threshold, accounts with expired/revoked logins, and accounts whose usage cannot be confirmed for both windows. The seat automation then verifies that the current member is Premium and the destination is Standard before changing either seat; credentials switch only after the Members page confirms the transfer. If every other account is over threshold or cannot be verified, no seat or account is changed. Disable rotation before changing the pool or editing seat tiers manually.

If `/acct:list` shows **🔴 Log in again** for an account, run the `/acct:login <name>` command it shows. Nothing else in the table needs your attention.

**Do not run `/logout` before switching.** It can revoke the token of the account you leave, and the saved copy stops working.

## How it works

- Claude Code keeps its login in the **Keychain** on macOS (item `Claude Code-credentials`) or in `~/.claude/.credentials.json` on Windows / Linux. The account email and organization are in `~/.claude.json`, key `oauthAccount`.
- `login` runs `claude auth login` with `CLAUDE_CONFIG_DIR` pointing at a temporary directory, so your current login is untouched. When it finishes, the token is moved into the store and the temporary directory is deleted. On macOS the current login is backed up first and restored automatically if the Keychain turns out not to be keyed by directory.
- `save` copies the current token into a private store: Keychain item `claude-acct` (macOS) or `~/.claude-acct/secrets/<name>.json` with mode 600 (Windows / Linux). The list and usage cache are in `~/.claude-acct/accounts.json`, which holds no tokens.
- `switch` first saves the **latest** token of the current account (Claude Code refreshes tokens over time), then writes the target account's token and `oauthAccount` where Claude Code reads them.
- A running Claude Code session compares its token with the stored one before each request, so it moves to the new account by itself. On macOS the Keychain read is cached for 30 seconds (`UOt=30000` in the Claude Code 2.1.29x bundle), so `switch` waits 31 seconds before it confirms; once it does, your next message in the same chat uses the new account. `--no-wait` skips the wait. On Windows / Linux the credentials file is re-read as soon as it changes, so there is no wait.
- `switch` refuses when the current account is not saved, so you cannot lose that login by accident (`--force` skips the check). It also refuses to switch to an account that needs a new login.
- Tokens are **never** printed into the conversation. Each command runs the script directly (`disable-model-invocation`); Claude only shows the output.

## When an account needs a new login

An account is flagged **🔴 Log in again** when its refresh token has expired (`refreshTokenExpiresAt`) or the usage API rejects a still-valid token with HTTP 401 (the token was revoked). An expired short-lived access token is **not** flagged: Claude Code refreshes it automatically the next time you switch to that account.

## Limitations

- The 30-second Keychain cache comes from reading Claude Code's own code, not from documentation, and may change in a future version.
- **Usage comes from an unofficial API** (`api.anthropic.com/api/oauth/usage`, the same data as `/usage`). Anthropic can change it at any time. Results are cached for 2 minutes to avoid HTTP 429.
- For an account you have not used for a while, the access token is expired, so `list` shows the last known usage with its age. The script does not refresh tokens itself, to avoid breaking the login. Switching to the account once brings fresh numbers.
- The claude.ai login can ask you to paste a code, and the chat box cannot take input, so `/acct:login` opens a separate terminal window (macOS: Terminal; Windows: a Git Bash window; Linux: the default terminal). If none can be opened, it prints the command to run yourself.
- In the VS Code extension, plugin commands may not appear in the `/` suggestion menu (known Claude Code issue). Typing the full command, e.g. `/acct:list`, works.
- API key, Bedrock and Vertex logins are not supported.
- Seat management depends on the current claude.ai Members page and its UI; Anthropic can change it. The automation requires an owner/admin browser session and a local Chrome CDP endpoint. Rotation runs at Claude Code hook boundaries, so it cannot interrupt an already-running request. `StopFailure` can trigger a safe seat/account rotation after a usage error, but plugin hooks cannot themselves replay a failed model request; unattended continuation depends on Claude Code's own auto-continue support and session behavior.
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
