# xtts-claude-account-pool (`acct`)

A Claude Code plugin for the XTGroup claude.ai Team. Save the team's accounts, see the 5-hour / 7-day usage of all of them in one table, switch between them **without logging out and back in**, and share one Premium seat: one always-on machine (machine 2) moves the seat to the account that was Premium least recently, and every working machine switches to the account that holds it. Works in the CLI and the VS Code extension, on macOS, Windows and Linux (the seat-moving service runs on macOS).

| Command | What it does |
|---|---|
| `/acct:login` | Log in, one after the other in one terminal window, to every pool account this machine does not have yet (or whose login expired) |
| `/acct:login <email>` or `/acct:login <name> [email]` | Log in to any account (new, or again) and save it. With just an email, the email is the account name, and the script checks that you signed in with that email in the browser. Opens a terminal window to finish the login; **your current account is not changed** |
| `/acct:save <name>` | Save the account you are **currently logged in** with, no new login needed |
| `/acct:list [--refresh]` | Table of saved accounts: email, organization, plan, organization seat (Premium / Standard / No seat), 5-hour / 7-day usage with reset times. Accounts that need a new login are flagged. `▶` = current account. It also shows the pool phase, the machines seen in the last 5 minutes, whether the Premium account has reached its threshold, and which account would take the seat next, or why each account is skipped |
| `/acct:switch <name> [--no-wait]` | Switch to another saved account and **keep working in the same chat**. Accepts the name, the email, or the part before `@` (e.g. `alice`) if it is unique |
| `/acct:auto join <key>` | Join the pool server (every machine, once): turns automatic account switching on |
| `/acct:auto mover <mover key>` | Make this machine the one that moves the Premium seat (machine 2, once); `off` removes it |
| `/acct:auto <enable|disable|status|browser>` | Turn switching on or off on this machine, show the pool state, open the admin browser (machine 2) |
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

**4. Log in to the pool accounts.** The twelve accounts of the pool are fixed in the plugin (see [The XTGroup pool](#the-xtgroup-pool)). One command goes through the ones this machine lacks:

```bash
$ACCT login
```

It opens a terminal window. For each account, tell the user to be signed in to claude.ai **as that account** in the browser (sign out of another one first), press Enter in the terminal, finish the login, and paste the code into the terminal if the page shows one. Afterwards, check with `$ACCT list --refresh`: every account should be listed without "🔴 Log in again".

**5. Join the pool.** Ask the user for the pool API key (they get it from the team, never from git) and run:

```bash
$ACCT auto join <key>
```

**6. Only on machine 2** (the always-on Mac that moves the seat): open the automation browser and have the user sign in to claude.ai there as an organization owner or admin, then install the service with the mover key:

```bash
$ACCT auto browser
$ACCT auto mover <mover key>
```

**7. Check.** Run `$ACCT doctor` and `$ACCT auto status` and show the user the result.

**8. Finish.** Tell the user to start a new session so the `/acct:*` commands and the hooks load: in the terminal, exit and run `claude` again; in VS Code, run **Developer: Reload Window**. In the new session, `/acct:doctor` should end with "Everything is ready".

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

1. `/acct:login`: log in to every pool account, one after the other.
2. `/acct:auto join <key>` with the pool API key you were given.
3. Machine 2 only: `/acct:auto browser` (sign in as an organization owner/admin), then `/acct:auto mover <mover key>`.
4. `/acct:doctor`: every line should be ✅.

The account tokens, the browser profile, its sign-in and the pool keys are local to each machine (under `~/.claude-acct`). On a new machine, repeat the steps; nothing is transferred.

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

### The XTGroup pool

Two words with two meanings, never mixed:

- **Move the seat**: change the seat tier of two accounts on the organization's Members page, P (Premium now) to No seat assigned and the target to Premium (the organization has one Premium and one Standard seat). It affects the whole organization and needs an admin browser. **Only the service on machine 2 does it.**
- **Switch account**: change the account Claude Code on one machine is logged in with. It affects only that machine. **The hooks of each working machine do it for that machine.**

The accounts are fixed in `scripts/claude_acct.py`:

| | Account | Role |
|---|---|---|
| — | `info@`, `careers@`, `support@xtgroup.vn` | Backup accounts, never Premium. The one in use holds the Standard seat: every working machine waits on it while the Premium seat moves. Once its 5-hour reaches 85% or a window is used up (and nothing is moving), machine 2 gives the Standard seat to the next one in this order (after support, info again), so the ones at rest can reset. Every working machine must be logged in to all three |
| 1–9 | `xtts@`, `sales@`, `contact@`, `hieunx@`, `truongnv@`, `thanglt@`, `kienlt@`, `xtg1@`, `xtg2@xtgroup.vn` | Take turns holding Premium; without the seat they have no seat at all; the order breaks ties |

```
Move the seat when (machine 2):  P.5h ≥ 80%  OR  P.7d ≥ 90%   (or any window at 100%)
Target (the next Premium):      Premium least recently (never first) → fixed order
                                (usage is not read: accounts without a seat have none; skips accounts
                                 not logged in on machine 2 or with an expired login)
No target:                      P keeps the seat, the reason is logged
```

**Pool server.** `server/pool_server.py` runs on the xtts server (port 5680, `https://xtts.xtgroup.vn/pool/` through nginx) and holds the shared phase (`STABLE`, `PREPARING`, `MOVING`), the Premium account, the target, the machines that report, and the log. Its dashboard (password from `/etc/account-pool.env`) shows all of it and refreshes every 5 seconds. Deploy it with `bash server/deploy.sh`; the API key, the mover key and the dashboard password are created on the server the first time and never stored in git.

**One seat move, step by step.**

1. Machine 2 (every 30 s, launchd): reads the usage of P. Below the thresholds, nothing happens.
2. Threshold reached: machine 2 picks the target and writes `PREPARING (P → target)`.
3. Every working machine, before its next request: sees `PREPARING`, **switches account to the backup account** and reports it.
4. Machine 2 waits until every machine seen in the last 5 minutes reports the backup account (at most 120 s; idle machines are not waited for).
5. Machine 2 writes `MOVING`, reads the seat of P again, and **moves the seat** in Chrome: P to No seat assigned, then the target to Premium, both checked on the page. A failure rolls both back.
6. Machine 2 writes `STABLE, premium = target`.
7. Every working machine, before its next request: **switches account to the new Premium**. An idle machine does it before its first request when it wakes up, so no request is ever sent with the old account.

If the move fails, machine 2 writes `STABLE` with the account that really holds the seat (P after the rollback), the working machines switch back to it, and machine 2 waits 15 minutes before it tries again; the dashboard shows the error (for example, the admin browser needs a new sign-in). If the pool server cannot be reached, a working machine follows the seat it reads on claude.ai, and machine 2 does not move the seat. A request that is already running always finishes with the account it started with.

**Machine 2.** An always-on machine that never sleeps, with a user logged in (the service needs that session's credentials and Chrome), all twelve accounts logged in (`/acct:login`), the admin browser signed in (`/acct:auto browser`), and the service installed by `/acct:auto mover <mover key>`. It never switches account, starts Chrome in the background, and does not need Claude Code to be open.

- macOS: a launch agent (`vn.xtgroup.acct-mover`) runs `acct.sh auto serve-once` every 30 seconds through `~/.claude-acct/bin/acct-mover.sh`, which always starts the installed plugin version.
- Windows (for example Windows Server over RDP): a Task Scheduler task (`XTTS_Acct_Mover`) starts at logon in the user's interactive session and runs `~/.claude-acct/bin/acct-mover.py`, a loop that calls `auto serve-once` every 30 seconds without opening a window (Task Scheduler cannot repeat faster than once a minute). Leave the RDP session with **Disconnect**, never **Sign out**, so the task, the logins and Chrome keep running. Output goes to `~/.claude-acct/mover.out.log`.

**Continuing after a usage-limit stop.** A long response can cross the limit before the next tool call, so the turn stops with a usage-limit error. The `StopFailure` hook runs with `asyncRewake`: it switches account the same way in the background, waits out the Keychain cache, then exits with code 2. Claude Code treats that as a wake-up and continues the stopped task in the same chat. If nothing was switched, the hook exits 0 and the session stays stopped.

Claude Code's own `autoContinueAtUsageLimit` (v2.1.234 or later) waits for the reset time of the account that hit the limit; it does not notice that the plugin switched to an account with usage left. The plugin's wake-up does not depend on it. Whether the two interact has not been verified on a real usage limit; turn `autoContinueAtUsageLimit` off if the wake-up is held.

**Rotation log.** Every decision and failure is written as one JSON line to `~/.claude-acct/rotation.log` and, once the machine has joined, uploaded in the background to the pool server (dashboard → Log): heartbeats, thresholds without a target and why each account was skipped, seat moves (start, success, failure, timeout waiting for the backup account), account switches, unreadable usage, usage-limit recoveries and any other error. Each line names the machine by `git config --global user.name` and carries the plugin version. No token or credential is ever logged.

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
