# xtts-claude-account-pool (`acct`)

A Claude Code plugin for people with more than one Claude account (claude.ai login). Save your accounts, see the 5-hour / 7-day usage of all of them in one table, and switch between them **without logging out and back in**. Works in the CLI and the VS Code extension, on macOS, Windows and Linux.

| Command | What it does |
|---|---|
| `/acct:login <email>` or `/acct:login <name> [email]` | Log in to any account (new, or again) and save it. With just an email, the email is the account name, and the script checks that you signed in with that email in the browser. Opens a terminal window to finish the login; **your current account is not changed** |
| `/acct:save <name>` | Save the account you are **currently logged in** with, no new login needed |
| `/acct:list [--refresh]` | Table of saved accounts: email, organization, plan, organization seat (Premium / Standard / No seat), 5-hour / 7-day usage with reset times. Accounts that need a new login are flagged. `▶` = current account |
| `/acct:switch <name> [--no-wait]` | Switch to another saved account and **keep working in the same chat**. Accepts the name, the email, or the part before `@` (e.g. `alice`) if it is unique |
| `/acct:auto <configure|enable|disable|status|check>` | Rotate the organization's Premium seat to the next saved account when the active Premium account reaches the configured usage threshold |
| `/acct:logout <name>` | Remove an account from the list (deletes its saved token) |
| `/acct:doctor` | Check the requirements, install the Node dependency, and list the setup steps that are left |

## Install

Requirements: Claude Code (CLI or VS Code extension) logged in with a claude.ai account, Python 3.8+, Node.js 20+ with npm, and Google Chrome. Every account of the pool must be a member of the same claude.ai Team organization, and one person must be able to sign in to that organization as an owner or admin (for the seat changes). On Windows, Claude Code runs the plugin's shell scripts through Git Bash.

Platform support:

| | Login storage | Account switch | Seat automation (Chrome) |
|---|---|---|---|
| macOS | Keychain | ✅ (waits 31 s for Claude Code's Keychain cache) | ✅ Google Chrome |
| Linux | `~/.claude/.credentials.json` | ✅ immediate | ✅ Google Chrome or Chromium, needs a desktop session (not a headless server) |
| Windows | `%USERPROFILE%\.claude\.credentials.json` | ✅ immediate | ✅ Google Chrome; requires Git for Windows (Claude Code needs it too) |

The unit and end-to-end tests run on all three systems with Python 3.8 and 3.12 (`.github/workflows/test.yml`). The parts that need a real claude.ai organization (seat changes on the Members page, a real usage limit) can only be checked by hand.

### The short way: ask Claude Code

Paste this into Claude Code:

```text
Install this plugin for me: https://github.com/lehieu252/xtts-claude-account-pool
```

Claude follows [Instructions for Claude Code](#instructions-for-claude-code) below. It installs everything it can, and asks you for the things only you can do: signing in to each account in the browser, and signing in once as an organization admin in the automation Chrome window.

### Instructions for Claude Code

These steps are written for the agent doing the install. Run them in order with the Bash tool and report each result to the user. Never print tokens or the contents of credential files.

**1. Check the requirements.**

```bash
python3 --version   # 3.8+ ("python" or "py -3" on Windows)
node --version      # v20+
npm --version
```

Check that Google Chrome is installed (macOS: `/Applications/Google Chrome.app`). If something is missing, tell the user what to install and how (for example `brew install node` on macOS, or https://nodejs.org), and ask before installing system software yourself.

**2. Add the marketplace and install the plugin.** `claude` may not be on `PATH` (for example with the VS Code extension only); inside Claude Code, `CLAUDE_CODE_EXECPATH` points to the running executable.

```bash
CLAUDE="${CLAUDE_CODE_EXECPATH:-claude}"
"$CLAUDE" plugin marketplace add lehieu252/xtts-claude-account-pool
"$CLAUDE" plugin install acct@xtts-claude-account-pool --scope user
```

If the marketplace or plugin is already there, run `"$CLAUDE" plugin marketplace update xtts-claude-account-pool` and `"$CLAUDE" plugin update acct@xtts-claude-account-pool` instead. If an older plugin also named `acct` from another marketplace is installed, uninstall it first (`"$CLAUDE" plugin list` shows it), because both would register the same `/acct:*` commands.

**3. Find the installed plugin and run the doctor.** The doctor installs the Node dependency (`playwright-core`) in the plugin directory and prints a checklist with the remaining steps.

```bash
PLUGIN_ROOT=$(python3 -c "import json,os; d=json.load(open(os.path.join(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude'),'plugins','installed_plugins.json'))); print(d['plugins']['acct@xtts-claude-account-pool'][0]['installPath'])")
echo "$PLUGIN_ROOT"
bash "$PLUGIN_ROOT/scripts/acct.sh" doctor
```

Shell variables do not survive between tool calls, so in the next steps `$ACCT` stands for `bash "<PLUGIN_ROOT>/scripts/acct.sh"` with the absolute path printed above written out in full. The slash commands and hooks of a newly installed plugin are only loaded by a new session, but the script works right away, so steps 4 to 7 run it directly in the current session.

**4. Save the accounts.** Ask the user for the email of every account in the pool. Save the account Claude Code is logged in with now, then log in to each other one:

```bash
$ACCT save <email of the current account>
$ACCT login <email>          # once per other account
```

`login` opens a separate terminal window and a browser login page. Tell the user to sign in **with that email** and, if the page shows a code, paste it into that terminal window. Wait until the user says it is done, then run the next one. The current Claude Code login is not changed. Afterwards, check:

```bash
$ACCT list --refresh
```

Every account should be listed without "🔴 Log in again", and the Seat column shows which one holds Premium now.

**5. Configure rotation.** Use the account whose Seat is Premium as `--premium`, and put it first in `--order`, followed by the other accounts in the order the user wants them to take over. The default threshold is 90% of the 5-hour usage; add `--threshold <1-99>` only if the user asks for another value.

```bash
$ACCT auto configure --premium <premium email> --order <premium email>,<next email>,<next email>
```

**6. Sign in to the automation browser.** This opens Chrome with a dedicated profile (`~/.claude-acct/browser-profile`), separate from the user's normal Chrome.

```bash
$ACCT auto browser
```

Tell the user to sign in to claude.ai in that window **as an organization owner or admin**, pass any Cloudflare check, and confirm that the Members page shows the expected Premium and Standard seats. Wait for the user to confirm. Leave the window open or closed as they like; the plugin starts it again when needed.

**7. Check and enable.** Run `$ACCT doctor` and `$ACCT auto status`, show the user the result, and turn rotation on only after the user agrees:

```bash
$ACCT auto enable
```

**8. Finish.** Tell the user to start a new session so the `/acct:*` commands and the rotation hooks load: in the terminal, exit and run `claude` again; in VS Code, run **Developer: Reload Window**. In the new session, `/acct:doctor` should end with "Everything is ready" and `/acct:list` shows the pool.

Rules: never run `/logout` (it can revoke a saved token), never edit seat tiers in the Members page while rotation is on, and do not enable rotation without the user's go-ahead.

### Manual install

The same steps by hand. In Claude Code (in the VS Code extension, `/plugins` opens the plugin manager):

```text
/plugin marketplace add lehieu252/xtts-claude-account-pool
/plugin install acct@xtts-claude-account-pool
```

Or from a terminal:

```bash
claude plugin marketplace add lehieu252/xtts-claude-account-pool
claude plugin install acct@xtts-claude-account-pool --scope user
```

Start a new session (VS Code: **Developer: Reload Window**), then:

1. `/acct:doctor`: checks the requirements, installs `playwright-core`, and lists what is left.
2. `/acct:save <email>` for the account you are logged in with, and `/acct:login <email>` for every other account of the pool. Each login opens a terminal window and a browser page; sign in with that email.
3. `/acct:list --refresh`: the Seat column shows which account holds Premium.
4. `/acct:auto configure --premium <premium email> --order <premium email>,<next>,<next>`
5. `/acct:auto browser`: sign in as an organization owner/admin in the Chrome window that opens.
6. `/acct:doctor` and `/acct:auto status`, then `/acct:auto enable`.

The account tokens, the browser profile and its sign-in are local to each machine (under `~/.claude-acct`). On a new machine, repeat the steps; nothing is transferred.

### Update

```bash
CLAUDE="${CLAUDE_CODE_EXECPATH:-claude}"
"$CLAUDE" plugin marketplace update xtts-claude-account-pool
"$CLAUDE" plugin update acct@xtts-claude-account-pool
```

Start a new session afterwards. Each version is installed in its own directory; the Node dependency is installed there automatically the first time seat automation runs (or by `/acct:doctor`). Saved accounts and settings in `~/.claude-acct` are kept.

## Switch accounts by hand

1. `/acct:save work`: save the account you are using now.
2. `/acct:login alice@example.com`: a terminal window and a browser login page open. Sign in with that account; if the page shows a code, paste it into the terminal.
3. `/acct:list`: see both accounts and their usage.

From then on, just run `/acct:switch work` or `/acct:switch alice`, wait for the confirmation, and keep working in the same chat. No new tab or `/resume` needed.

### Automatic Premium seat rotation

The rotation is opt-in. The default trigger is **90% of the 5-hour usage** of the Premium account. Configure the current Premium account and the order in which seats should rotate:

```
/acct:auto configure --premium alice@example.com --order alice@example.com,bob@example.com,carol@example.com
/acct:auto browser
/acct:auto status
/acct:auto enable
```

To change the threshold, for example for a test at 75%:

```
/acct:auto configure --premium alice@example.com --order alice@example.com,bob@example.com,carol@example.com --threshold 75
```

`--daily-threshold 75` is the same. Run `/acct:auto status` to see the active value. Re-running `configure` keeps rotation off until you enable it again.

**Why the 7-day window is not a threshold.** A member's 7-day usage is measured against the limits of the seat it holds. A Standard member at 100% of its 7-day window says nothing about how much it can do once it holds the Premium seat, so the plugin neither rotates away from Premium at a 7-day percentage nor skips a destination because of one. The only exception is a window that is **used up** (100%) on the Premium account itself: it cannot continue, so the plugin rotates, and it does not give that account the seat back until that window resets.

The plugin checks the active Premium account before each prompt and tool call. When its 5-hour usage reaches the threshold, or one of its windows is used up, it changes the old member to Standard, promotes the next eligible member to Premium in the Members page, verifies the displayed tiers, and then switches Claude Code to that account. If Claude Code reports a usage-related `rate_limit` failure, a `StopFailure` hook makes one best-effort rotation attempt using the same fresh-usage and seat checks. It ignores ordinary API throttling and other errors. Use `/acct:auto disable` to stop rotation.

**Continuing after a usage-limit stop.** A long response can cross the limit before the next tool call, so the turn stops with a usage-limit error. The `StopFailure` hook runs with `asyncRewake`: it rotates in the background, waits out the Keychain cache, then exits with code 2. Claude Code treats that as a wake-up and continues the stopped task in the same chat, with the same context and the new account. Nobody has to type "continue". If nothing was rotated (ordinary throttling, no eligible account, rotation off), the hook exits 0 and the session stays stopped. Each rotation wakes the model at most once, so the chain ends when the pool runs out.

Claude Code's own `autoContinueAtUsageLimit` (v2.1.234 or later) waits for the reset time of the account that hit the limit; it does not notice that the plugin switched to an account with usage left. The plugin's wake-up does not depend on it. Whether the two interact (for example, the wake-up being held until that reset while native auto-continue is waiting) has not been verified on a real usage limit. Test it once with your Claude Code version, and turn `autoContinueAtUsageLimit` off if the wake-up is held.

Run `/acct:auto browser` once to open the persistent automation profile in regular Chrome. Sign in as an organization owner/admin if needed. The plugin uses Chrome's local debugging endpoint to control this dedicated profile; your normal Chrome profile stays separate. Chrome 136 and later block remote debugging of the default user-data directory, so the plugin uses this dedicated profile. The Members page defaults to `https://claude.ai/admin-settings/members`; override it with `--members-url` if your organization uses a different path on claude.ai.

The account order is explicit and cyclic. Before choosing a destination, the plugin checks saved credentials and refreshes that account's usage (using the short cache to avoid excessive requests). It skips accounts whose 5-hour usage is at the threshold, accounts that ran out while holding Premium (until that window resets), accounts with expired/revoked logins, and accounts whose 5-hour usage cannot be confirmed. The seat automation then verifies that the current member is Premium and the destination is Standard before changing either seat; credentials switch only after the Members page confirms the transfer. If every other account is over threshold or cannot be verified, no seat or account is changed. Disable rotation before changing the pool or editing seat tiers manually.

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

## Troubleshooting

- **Usage shows "no data yet (error: …)" for every account**: the script cannot reach `api.anthropic.com`. Run `/acct:doctor` and look at the "HTTPS to api.anthropic.com" line. A Python installed from python.org on macOS has no CA certificates until you run `/Applications/Python 3.x/Install Certificates.command`; the plugin falls back to the macOS system root certificates (or `certifi` when installed) by itself, so this only remains when that fallback is not available. Otherwise check the network, proxy or firewall.
- **"no data yet" without an error**: the account has no usage reading yet; `/acct:list` fetches it (and refreshes an expired access token first). Plugin versions before 0.8.4 did not refresh tokens, so update the plugin.

## Limitations

- The 30-second Keychain cache comes from reading Claude Code's own code, not from documentation, and may change in a future version.
- **The Seat column comes from an unofficial API** (`api.anthropic.com/api/oauth/profile`, field `organization.seat_tier`): `team_tier_1` is shown as Premium and `team_standard` as Standard; any other value is shown as is. It needs a valid access token, like usage, so an account you have not used for a while shows its last known seat.
- **Usage comes from an unofficial API** (`api.anthropic.com/api/oauth/usage`, the same data as `/usage`). Anthropic can change it at any time. Results are cached for 2 minutes to avoid HTTP 429.
- Access tokens last about 8 hours. For a saved account that is **not** the one Claude Code is using, the script refreshes an expired access token itself (the same OAuth refresh Claude Code does, `platform.claude.com/v1/oauth/token`) and saves the new token, so `list` and rotation see current usage even for accounts that have been idle for days. It never refreshes the token of the account Claude Code is logged in with. If the refresh token is rejected, the account is flagged **🔴 Log in again**.
- The claude.ai login can ask you to paste a code, and the chat box cannot take input, so `/acct:login` opens a separate terminal window (macOS: Terminal; Windows: a Git Bash window; Linux: the default terminal). If none can be opened, it prints the command to run yourself.
- In the VS Code extension, plugin commands may not appear in the `/` suggestion menu (known Claude Code issue). Typing the full command, e.g. `/acct:list`, works.
- API key, Bedrock and Vertex logins are not supported.
- Seat management depends on the current claude.ai Members page and its UI; Anthropic can change it. The automation requires an owner/admin browser session and a local Chrome CDP endpoint. Rotation runs at Claude Code hook boundaries, so it cannot interrupt an already-running request. The continue-after-limit wake-up uses the `asyncRewake` hook option, verified against Claude Code 2.1.296 with a mock API that returns a usage-limit 429. It needs the session to stay open: `claude -p` exits before the background hook finishes.
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
