# xtts-claude-account-pool (`acct`)

A Claude Code plugin for the XTGroup claude.ai Team. Save the team's accounts, see the 5-hour / 7-day usage of all of them in one table, switch between them **without logging out and back in**, and share them as one pool: one always-on machine (machine 2) picks the account every machine works on (Premium while it has usage left, else a Standard account with a seat) and moves the Standard seats of a second Team when needed, and every working machine switches to that account. Works in the CLI and the VS Code extension, on macOS, Windows and Linux (the seat-moving service runs on macOS).

| Command | What it does |
|---|---|
| `/acct:login` | Log in, one after the other in one terminal window, to every pool account this machine does not have yet (or whose login expired). claude.ai refuses Claude Code to an account without a seat: log in the accounts of xtgroup.com.vn while they hold one (a seat given by hand counts against claude.ai's weekly limit). Open the login link in a private window and paste the whole code (Windows Git Bash: Shift+Insert) |
| `/acct:login <email>` or `/acct:login <name> [email]` | Log in to any account (new, or again) and save it. With just an email, the email is the account name, and the script checks that you signed in with that email in the browser. Opens a terminal window to finish the login; **your current account is not changed** |
| `/acct:save <name>` | Save the account you are **currently logged in** with, no new login needed |
| `/acct:list [--refresh]` | Table of saved accounts: email, organization, plan, organization seat (Premium / Standard / No seat), 5-hour / 7-day usage with reset times. Accounts that need a new login are flagged. `▶` = current account. It also shows the account every machine works on and why, the next ones, the accounts with a seat, and the machines seen in the last 5 minutes |
| `/acct:switch <name> [--no-wait]` | Switch to another saved account and **keep working in the same chat**. Accepts the name, the email, or the part before `@` (e.g. `alice`) if it is unique |
| `/acct:auto join <key>` | Join the pool server (every machine, once): turns automatic account switching on |
| `/acct:auto mover <mover key>` | Make this machine the one that picks the active account and moves the seats of xtgroup.com.vn (machine 2, once); `off` removes it |
| `/acct:auto <enable|disable|status|browser>` | Turn switching on or off on this machine, show the pool state, open the admin browser (machine 2) |
| `/acct:logout <name>` | Remove an account from the list (deletes its saved token) |
| `/acct:doctor` | Check the requirements, install the Node dependency, and list the setup steps that are left |

## Install

Requirements: Claude Code (CLI or VS Code extension) logged in with a claude.ai account, Python 3.8+, Node.js 20+ with npm, and Google Chrome. The accounts of the pool belong to the claude.ai Team organization xtgroup.com.vn (since 1.0.37; xtgroup.vn too before), and the admin browser on machine 2 must be signed in as an owner or admin of xtgroup.com.vn (for the seat changes). On Windows, Claude Code runs the plugin's shell scripts through Git Bash.

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

**4. Log in to the pool accounts.** The accounts of the pool are fixed in the plugin (see [The XTGroup pool](#the-xtgroup-pool)). One command goes through the ones this machine lacks:

```bash
$ACCT login
```

It opens a terminal window. For each account, tell the user to be signed in to claude.ai **as that account** in the browser (sign out of another one first), press Enter in the terminal, finish the login, and paste the code into the terminal if the page shows one. Afterwards, check with `$ACCT list --refresh`: every account should be listed without "🔴 Log in again".

**5. Join the pool.** Ask the user for the pool API key (they get it from the team, never from git) and run:

```bash
$ACCT auto join <key>
```

**6. Only on machine 2** (the always-on machine that runs the pool service): open the automation browser and have the user sign in to claude.ai there as an owner or admin of xtgroup.com.vn, then install the service with the mover key:

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
3. Machine 2 only: `/acct:auto browser` (sign in as an owner/admin of xtgroup.com.vn), then `/acct:auto mover <mover key>`.
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

- **Switch account**: change the account Claude Code on one machine is logged in with. It affects only that machine. **The hooks of each working machine do it for that machine**, to the account machine 2 publishes as active.
- **Move a seat**: change the seat tier of accounts on an organization's Members page. It affects the whole organization and needs an admin browser. **Only the service on machine 2 does it, and only in xtgroup.com.vn.**

**Since 1.0.37 (23/11/2026) the pool is xtgroup.com.vn only** (`POOL_ORGS`): its four accounts (`xtg@`, `xtg1@`, `xtg2@`, `xtg3@`) each hold a Standard seat, there is no Premium account, and the accounts of xtgroup.vn (`sales@`, `truongnv@`, `xtg2@`) are no longer used: a machine still on one goes to the active account. Every switch between two organizations makes Claude Code rewrite the whole conversation into the prompt cache (about 350k tokens in a long session) and drop its earlier thinking ("Claude can't use thinking from another organization"), so staying in one Team also saves usage. The rules below are those of the pool with both Teams (they apply again if `"xtgroup.vn"` is put back into `POOL_ORGS`); with xtgroup.com.vn only, machine 2 takes the Standard rules: keep the current account until it reaches a threshold, then the best ranked one below them (lowest 7d, then 5h, …), and when every account is at a threshold the current one, unless it used a window up.

**Token statistics (1.0.40).** The dashboard has a Token panel: for today (Vietnam time) the calls, input, output, cache written, new tokens (input + output + cache written) and cache read, each against the day before, for all machines or one; a stacked bar chart of the new tokens of the last 7 days per machine (hover a day for each machine's part); and the machines side by side, most tokens today first. Each machine that joined the pool counts them itself, from the transcripts Claude Code keeps (`~/.claude/projects/**/*.jsonl`, subagents included): every hook starts the count in the background at most every 10 minutes (`TOKEN_STATS_EVERY`), switching on or off, and sends the last 8 days to the pool server (`POST /api/tokens`). It asks nothing of Claude or of the usage API, so it costs no token, and needs no setup beyond updating the plugin. A message is written 1–5 times in a transcript with the same usage, so it is counted once by its id. A machine only sends while Claude Code runs on it (it counts the days it missed on its next run); a machine on an older version is listed as not sending.

**Thresholds (1.0.39).** A Standard account is at a threshold at 5-hour ≥ 90 % or 7-day ≥ 95 % (`STANDARD_DAILY_THRESHOLD`, `STANDARD_WEEKLY_THRESHOLD`), so near one 10 points below that; Premium and the Claude Max reserve stay at 80 %/90 % (`DAILY_THRESHOLD`, `WEEKLY_THRESHOLD`). The dashboard colours each account's bars by its own thresholds. The figures in the rules below are those of 1.0.38 and before.

**The Claude Max reserve (1.0.38).** `RESERVE_EMAILS` holds one more account, a personal Claude Max plan (no organization seat, so there is no seat to read: only its usage). The machines work on it only while **no Standard account is below the thresholds**, and leave it for a Standard account the moment one is: a window only goes down when it resets, so this does not flip back and forth. Order: the account picked on the dashboard, the current Standard account while below the thresholds, the best ranked Standard account below them, then the reserve while it is below them too, and when everything is at a threshold the current account stays unless it used a window up. The reserve is the last of the fallbacks, is shown in its own group on the dashboard, and every machine must be logged in to it (`/acct:login`). Each switch to or from it crosses organizations, so Claude Code rewrites the conversation into the prompt cache and drops its earlier thinking (see above): the plugin switches only when it must. One consumer Max plan shared by several machines (and people) may be against Anthropic's consumer terms, or be rate limited.

claude.ai limits, per organization and seat type, how many people may use a seat type in a week and how often a seat may pass from one person to another ("This organization has reached its weekly limit on moving this seat type between people … You can try again after <date> (UTC)"; the week starts Monday 00:00 UTC). On 17–18/11/2026 both limits were used up in xtgroup.vn: an open seat then only went back to its last few holders. So since 1.0.33 the pool spans two claude.ai Teams, and the machines switch between accounts that already hold a seat instead of moving the Premium seat. The accounts are fixed in `scripts/claude_acct.py`:

| Organization | Accounts | Seats |
|---|---|---|
| xtgroup.vn | `sales@` (`PREMIUM_EMAIL`) | Premium, held for good |
| xtgroup.vn | `truongnv@`, `xtg2@` (`FIXED_STANDARD_EMAILS`) | Standard, held for good |
| xtgroup.com.vn | `xtg@`, `xtg1@`, `xtg2@`, `xtg3@` (`SWAP_EMAILS`) | 2 Standard seats: machine 2 moves one only when it is needed |

Every working machine must be logged in to the pool accounts (four since 1.0.37; seven with both Teams, when the same name before `@` exists in both organizations and `/acct:switch` needs the whole email). claude.ai refuses Claude Code to an account without a seat, so log in the accounts of xtgroup.com.vn while they hold one; every seat given by hand counts against the weekly limit. The accounts of former pools (`xtts@`, `contact@`, `xtg1@`, `xtg6@`, `info@xtgroup.vn` …) are no longer used: a machine still on one goes to the active account.

Each account is read against the limits of its own seat (a Standard seat has about a fifth of a Premium one).

```
At a threshold:   5h ≥ 80%  OR  7d ≥ 90%   (or a window at 100%)
Near a threshold: 5h ≥ 70%  OR  7d ≥ 80%   (PREPARE_MARGIN = 10 points below)

Active account (every machine works on it), machine 2 picks it every 30 s:
  1. the account picked on the dashboard ("Dùng"), while it is below the thresholds
  2. Premium, while it is below the thresholds
  3. the current Standard account, while it is below them (no switching back and forth)
  4. the best ranked Standard account with a seat below them: lowest 7d → lowest 5h → earliest 5h
     reset → earliest 7d reset → the order above
  5. none below them: the current one, else the best ranked one, unless it used a window up
  Premium back below the thresholds (its window reset): every machine returns to it at once.
  Only accounts with their seat, logged in on machine 2 with a valid login and a known usage count.

Move a seat of xtgroup.com.vn when ALL of:
  - the active account is near a threshold
  - no other account with a seat (either organization) is below the near level
  - Premium is not back within 30 minutes (PREMIUM_SOON)
  - claude.ai has not refused a seat move for a weekly limit (then: none until the date it gives),
    and the last move did not fail in the last 15 minutes
  From: the account of xtgroup.com.vn with a seat that has the least left, never the active one
  To:   the account of xtgroup.com.vn without a seat with the most room: its last reading with a window
        past its reset at 0%, never read = 0%; it must be below the near level
  The new account holds the seat before the machines need it: a machine only ever switches to an
  account that holds a seat already.
```

**Seat moves are off since 1.0.34** (`SEAT_MOVES = False`): how many moves claude.ai allows a week is not known, so machine 2 moves no seat and the machines only switch between the accounts that hold one. A seat given by hand on the Members page is used at once (machine 2 reads every seat every 3 minutes), and the dashboard shows no "Gán seat" button. Only the accounts that hold a seat are ever chosen. Since 19/11 (1.0.36) xtgroup.com.vn has 4 Standard seats, one per account, so every pool account (`ALWAYS_EMAILS = POOL_EMAILS`) are listed on the dashboard and asked for by `/acct:login` and `/acct:doctor`, even before machine 2 is logged in to them (it cannot read the seat of an account it has no login for). An account without a seat is listed as "No seat" and never chosen. Narrowing `ALWAYS_EMAILS` to the three fixed accounts of xtgroup.vn (1.0.35) lists and asks for only the accounts that hold a seat (machine 2 publishes the list). `"seatMoves": true` in machine 2's `~/.claude-acct/rotation.json` turns the rules above back on, without a new version.

Machine 2 reads the seat of every account and the usage of every account with a seat every 3 minutes (the usage API limits each account), and keeps on the pool server the last reading of each one, which ranks it once it has no seat. It counts the seat moves of the week (Monday 00:00 UTC) and shows them on the dashboard.

**Seat moves through the API.** `scripts/seat_manager.mjs` opens a tab of its own in the admin Chrome and calls the API the Members page itself uses (`PUT /api/organizations/<org>/members/<account>` with `seat_tier` `team_standard` / `unassigned`), then reads the members again to check. claude.ai answers at once, with its reason when it refuses (the weekly limit and its date, no seat free, …); a failed step rolls back the ones before it. Before 1.0.33 it clicked the page's seat pickers, which broke whenever the page changed (17/11/2026: "Tier for" became "Seat type for"). The organization id of xtgroup.com.vn comes from the saved login of one of its accounts on machine 2; the Chrome account must be an owner or admin of xtgroup.com.vn.

**Dashboard buttons.** "Dùng" on an account with a seat puts every machine on it until it reaches a threshold (then machine 2 picks again). "Gán seat" on an account of xtgroup.com.vn without a seat gives it a Standard seat, taken from the account of xtgroup.com.vn with a seat that has the least left (never the active one); it counts against the weekly limit. The account that would get a seat next is tagged "Seat tiếp theo". One request at a time; machine 2 does it on its next run (30 s at most) and the dashboard shows its progress and any error. Anyone with the dashboard password can use them.

**Pool server.** `server/pool_server.py` runs on the xtts server (port 5680, `https://xtts.xtgroup.vn/pool/` through nginx) and holds the active account, the accounts a machine may use when it lacks that one (`fallbacks`, below the thresholds, best first), the accounts with a seat, the machines that report, the usage table and the log. Its dashboard (password from `/etc/account-pool.env`) shows all of it and refreshes every 5 seconds. Deploy it with `bash server/deploy.sh`; the API key, the mover key and the dashboard password are created on the server the first time and never stored in git.

**Working machines.** Before each prompt and tool call, the hook reads the pool state (cached 5 s) and switches to the active account, or, when it is not logged in on this machine, to the first fallback that is (and logs that the active one is missing). If the pool server cannot be reached, the machine stays where it is. A request that is already running always finishes with the account it started with.

**Machine 2.** An always-on machine that never sleeps, with a user logged in (the service needs that session's credentials and Chrome), every pool account logged in (`/acct:login`), the admin browser signed in as an owner or admin of xtgroup.com.vn (`/acct:auto browser`), and the service installed by `/acct:auto mover <mover key>`. It never switches account, starts Chrome in the background, and does not need Claude Code to be open.

- macOS: a launch agent (`vn.xtgroup.acct-mover`) runs `acct.sh auto serve-once` every 30 seconds through `~/.claude-acct/bin/acct-mover.sh`, which always starts the installed plugin version.
- Windows (for example Windows Server over RDP): a Task Scheduler task (`XTTS_Acct_Mover`) starts at logon in the user's interactive session and runs `~/.claude-acct/bin/acct-mover.py`, a loop that calls `auto serve-once` every 30 seconds without opening a window (Task Scheduler cannot repeat faster than once a minute). Leave the RDP session with **Disconnect**, never **Sign out**, so the task, the logins and Chrome keep running. Output goes to `~/.claude-acct/mover.out.log`.

**Continuing after a usage-limit stop.** A long response can cross the limit before the next tool call, so the turn stops with a usage-limit error. The `StopFailure` hook runs with `asyncRewake`: it switches account the same way in the background, waits out the Keychain cache, then exits with code 2. Claude Code treats that as a wake-up and continues the stopped task in the same chat. If nothing was switched, the hook exits 0 and the session stays stopped.

Claude Code's own `autoContinueAtUsageLimit` (v2.1.234 or later) waits for the reset time of the account that hit the limit; it does not notice that the plugin switched to an account with usage left. The plugin's wake-up does not depend on it. Whether the two interact has not been verified on a real usage limit; turn `autoContinueAtUsageLimit` off if the wake-up is held.

**Rotation log.** Every decision and failure is written as one JSON line to `~/.claude-acct/rotation.log` and, once the machine has joined, uploaded in the background to the pool server: heartbeats, the active account changing (`active`), seat moves (`seat_swap`, `seat_swap_ok`, `seat_swap_fail`, `swap_blocked`, `no_swap`), dashboard requests, account switches, unreadable usage, usage-limit recoveries and any other error. Each line names the machine by `git config --global user.name` and carries the plugin version. No token or credential is ever logged.

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

A login lasts about **4 weeks (28–30 days) from the moment you log in**; using the account or refreshing its access token (about every 8 hours) does not extend it. These figures were observed, not documented. A week before a login ends, `/acct:list` flags it **🟠 Login ends in N day(s)** and the dashboard lists it (logins saved on machine 2), so it can be renewed with `/acct:login <email>` before it stops a seat move. The dashboard also lists the accounts machine 2 must log in to again.

## Troubleshooting

- **"claude.ai refuses seat moves until …" on the dashboard**: claude.ai refused a seat move for a weekly limit of xtgroup.com.vn (`seat_swap_fail` in the log has its message). Machine 2 moves no seat until the date claude.ai gave; the machines go on with the accounts that hold a seat. "Gán seat" is refused meanwhile.
- **"The admin browser's account cannot manage organization …"**: the account signed in to the admin Chrome on machine 2 is not an owner or admin of xtgroup.com.vn. Sign it in again with `/acct:auto browser`, or invite that account to xtgroup.com.vn as an admin.
- **Usage shows "no data yet (error: …)" for every account**: the script cannot reach `api.anthropic.com`. Run `/acct:doctor` and look at the "HTTPS to api.anthropic.com" line. A Python installed from python.org on macOS has no CA certificates until you run `/Applications/Python 3.x/Install Certificates.command`; the plugin falls back to the macOS system root certificates (or `certifi` when installed) by itself, so this only remains when that fallback is not available. Otherwise check the network, proxy or firewall.
- **"no data yet" without an error**: the account has no usage reading yet; `/acct:list` fetches it (and refreshes an expired access token first). Plugin versions before 0.8.4 did not refresh tokens, so update the plugin.

## Limitations

- The 30-second Keychain cache comes from reading Claude Code's own code, not from documentation, and may change in a future version.
- **The Seat column comes from an unofficial API** (`api.anthropic.com/api/oauth/profile`, field `organization.seat_tier`): `team_tier_1` is shown as Premium and `team_standard` as Standard; any other value is shown as is. It needs a valid access token, like usage, so an account you have not used for a while shows its last known seat.
- **Usage comes from an unofficial API** (`api.anthropic.com/api/oauth/usage`, the same data as `/usage`). Anthropic can change it at any time. Results are cached for 3 minutes to avoid HTTP 429. While it limits an account it answers 429 with `Retry-After: 0`; the plugin then does not ask again for 5 minutes (as Claude Code does), and doubles the wait at each further 429 in a row, up to 30 minutes. The seat reading (`/api/oauth/profile`) follows the same rule.
- Access tokens last about 8 hours. For a saved account that is **not** the one Claude Code is using, the script refreshes an expired access token itself (the same OAuth refresh Claude Code does, `platform.claude.com/v1/oauth/token`) and saves the new token, so `list` and rotation see current usage even for accounts that have been idle for days. It never refreshes the token of the account Claude Code is logged in with. If the refresh token is rejected, the account is flagged **🔴 Log in again**.
- The claude.ai login can ask you to paste a code, and the chat box cannot take input, so `/acct:login` opens a separate terminal window (macOS: Terminal; Windows: a Git Bash window; Linux: the default terminal). If none can be opened, it prints the command to run yourself.
- In the VS Code extension, plugin commands may not appear in the `/` suggestion menu (known Claude Code issue). Typing the full command, e.g. `/acct:list`, works.
- API key, Bedrock and Vertex logins are not supported.
- Seat changes use the unofficial API of the claude.ai Members page (`/api/organizations/<org>/members`); Anthropic can change it. They require an owner/admin browser session and a local Chrome CDP endpoint. Rotation runs at Claude Code hook boundaries, so it cannot interrupt an already-running request. The continue-after-limit wake-up uses the `asyncRewake` hook option, verified against Claude Code 2.1.296 with a mock API that returns a usage-limit 429. It needs the session to stay open: `claude -p` exits before the background hook finishes.
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
