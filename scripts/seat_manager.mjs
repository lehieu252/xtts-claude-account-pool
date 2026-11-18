import { chromium } from "playwright-core";
import { spawn } from "node:child_process";

// seat_manager.mjs --org <id> <email>=<tier> ...   change seats of that organization, in order (tier: Premium,
//                                                  Standard or "No seat"); a failed step rolls back the ones before
// seat_manager.mjs --org <id> --read <email> ...   print {"<email>": "Premium" | "Standard" | "No seat"}
// seat_manager.mjs --setup                         open the admin Chrome profile to sign in
//
// Through the API the claude.ai Members page itself calls (PUT /api/organizations/<org>/members/<account>), from a
// tab of its own in the admin Chrome (the signed-in admin's session). The page's controls change now and then
// (17/11/2026: the "Tier for" pickers became "Seat type for" and every click timed out); the API answers at once,
// with claude.ai's reason when it refuses a change, e.g. "This organization has reached its weekly limit on moving
// this seat type between people. ... You can try again after 2026-11-24 (UTC)".
const TIERS = { "Premium": "team_tier_1", "Standard": "team_standard", "No seat": "unassigned" };
const NAMES = Object.fromEntries(Object.entries(TIERS).map(([k, v]) => [v, k]));
const argv = process.argv.slice(2);
const setupMode = argv[0] === "--setup";
const orgAt = argv.indexOf("--org");
const org = orgAt >= 0 ? argv[orgAt + 1] : null;
const rest = argv.filter((_, i) => orgAt < 0 || (i !== orgAt && i !== orgAt + 1));
const readEmails = rest[0] === "--read" ? rest.slice(1).map((e) => e.toLowerCase()) : null;
const changes = readEmails ? [] : rest.filter((a) => a.includes("=")).map((a) => {
  const at = a.lastIndexOf("=");
  return { email: a.slice(0, at).toLowerCase(), tier: a.slice(at + 1) };
});
const membersUrl = process.env.CLAUDE_ACCT_MEMBERS_URL || "https://claude.ai/admin-settings/members";
const cdpUrl = process.env.CLAUDE_ACCT_CDP_URL || "http://127.0.0.1:9222";
const browserProfile = process.env.CLAUDE_ACCT_BROWSER_PROFILE;

const fail = (message) => { console.error(message); process.exit(1); };
if (!setupMode) {
  if (!org || !/^[0-9a-f-]{36}$/i.test(org)) fail("Expected --org <organization id>.");
  if (!readEmails?.length && (!changes.length || changes.some((c) => !c.email || !TIERS[c.tier]))) {
    fail('Expected <email>=<tier> arguments, with tier Premium, Standard or "No seat".');
  }
}
const members = new URL(membersUrl);
if (members.protocol !== "https:" || members.hostname !== "claude.ai") fail("Members URL must be an HTTPS claude.ai page.");
const cdp = new URL(cdpUrl);
if (!((cdp.hostname === "127.0.0.1" || cdp.hostname === "localhost" || cdp.hostname === "::1") &&
      (cdp.protocol === "http:" || cdp.protocol === "https:"))) {
  fail("Playwright CDP must point to localhost.");
}

const versionUrl = new URL("/json/version", cdpUrl).toString();
const hasCdp = async () => {
  try { return (await fetch(versionUrl, { signal: AbortSignal.timeout(700) })).ok; }
  catch { return false; }
};
const startRegularChrome = async () => {
  if (!browserProfile) fail(`No Chrome CDP endpoint at ${cdpUrl}.`);
  const args = [
    `--user-data-dir=${browserProfile}`,
    `--remote-debugging-port=${cdp.port || "9222"}`,
    "--no-first-run",
    "--new-window",
    // Keep working when the window is covered or the RDP session is disconnected.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    membersUrl,
  ];
  let command, commandArgs;
  if (process.platform === "darwin") {
    command = "open";
    commandArgs = ["-gna", "Google Chrome", "--args", ...args];  // -g: stay in the background
  } else {
    command = process.env.CLAUDE_ACCT_CHROME || (process.platform === "win32" ? "chrome.exe" : "google-chrome");
    commandArgs = args;
  }
  const child = spawn(command, commandArgs, { detached: true, stdio: "ignore" });
  child.on("error", () => {});
  child.unref();
  for (let attempt = 0; attempt < 50; attempt++) {
    if (await hasCdp()) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  fail("Could not connect to Chrome. Close any other window using the automation profile, then run /acct:auto browser again.");
};

if (!(await hasCdp())) await startRegularChrome();
const browser = await chromium.connectOverCDP(cdpUrl, { timeout: 10000, noDefaults: true });
const context = browser.contexts()[0];
if (!context) fail("No browser context found at the CDP endpoint.");
if (setupMode) {
  const page = context.pages().find((p) => p.url().startsWith("https://claude.ai/")) || await context.newPage();
  await page.goto(membersUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  console.log("Chrome is using the saved automation profile. Sign in as an organization owner/admin if needed, then return here; keep the browser open for automatic seat changes.");
  await new Promise((resolve) => browser.on("disconnected", resolve));
  process.exit(0);
}

// A tab of its own, closed at the end (the user's tabs are left alone). The page's own API calls carry a few
// anthropic-* headers: reuse them, so the calls below look like the page's.
const page = await context.newPage();
const done = async (code) => { await page.close().catch(() => {}); process.exit(code); };
let headers = null;
page.on("request", (q) => {
  if (!headers && /\/api\/organizations\//.test(q.url())) {
    headers = Object.fromEntries(Object.entries(q.headers()).filter(([k]) => /^(anthropic-|x-)/i.test(k)));
  }
});
try {
  await page.goto(membersUrl, { waitUntil: "domcontentloaded", timeout: 60000 });
} catch (error) {
  console.error(`Could not open ${membersUrl}: ${String(error.message || error).split("\n")[0]}`);
  await done(1);
}
for (let i = 0; i < 40 && !headers && !/login|signin/i.test(page.url()); i++) await page.waitForTimeout(500);
if (/login|signin/i.test(page.url())) {
  console.error("The admin browser is not signed in to claude.ai. Sign in as an owner or admin of the organization (/acct:auto browser) and retry.");
  await done(1);
}

const call = (method, path, body) => page.evaluate(async ([method, url, body, headers]) => {
  try {
    const r = await fetch(url, { method, credentials: "include",
      headers: { ...headers, "content-type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    let json = null;
    try { json = await r.json(); } catch {}
    return { status: r.status, json };
  } catch (error) {
    return { status: 0, json: null, error: String(error) };
  }
}, [method, `/api/organizations/${org}${path}`, body, headers || {}]);

// claude.ai's reason for a refusal: "<message> [<error_code>/<limit>]".
const reason = (r) => {
  const e = (r.json && r.json.error) || {};
  const d = e.details || {};
  const code = [d.error_code || e.type, d.limit].filter(Boolean).join("/");
  return `${e.message || r.error || "HTTP " + r.status}${code ? ` [${code}]` : ""}`;
};

const readMembers = async () => {
  const out = {};
  for (let offset = 0; offset < 2000; offset += 100) {
    const r = await call("GET", `/members_v2?offset=${offset}&limit=100&types%5B%5D=member`);
    if (r.status === 403 || r.status === 404) {
      throw new Error(`The admin browser's account cannot manage organization ${org} (HTTP ${r.status}): sign it in as an owner or admin of that organization.`);
    }
    if (r.status !== 200 || !r.json || !Array.isArray(r.json.data)) throw new Error(`Could not read the members: ${reason(r)}`);
    for (const d of r.json.data) {
      const m = d.member || d;
      const email = (m.account && m.account.email_address || "").toLowerCase();
      if (email) out[email] = { uuid: m.account.uuid, tier: NAMES[m.seat_tier] || "No seat" };
    }
    if (r.json.data.length < 100) break;
  }
  return out;
};

const put = async (who, email, tier) => {
  const r = await call("PUT", `/members/${who[email].uuid}`, { seat_tier: TIERS[tier] });
  if (r.status !== 200) throw new Error(`${email} -> ${tier}: ${reason(r)}`);
};

let list;
try {
  list = await readMembers();
  const unknown = (readEmails || changes.map((c) => c.email)).filter((e) => !list[e]);
  if (unknown.length) throw new Error(`Not members of organization ${org}: ${unknown.join(", ")}.`);
} catch (error) {
  console.error(error.message);
  await done(1);
}
if (readEmails) {
  console.log(JSON.stringify(Object.fromEntries(readEmails.map((e) => [e, list[e].tier]))));
  await done(0);
}

const made = [];
try {
  for (const { email, tier } of changes) {
    if (list[email].tier === tier) continue;
    await put(list, email, tier);
    made.push({ email, before: list[email].tier });
  }
  const now = await readMembers();
  const off = changes.filter((c) => now[c.email].tier !== c.tier);
  if (off.length) throw new Error(`claude.ai accepted the change but shows ${off.map((c) => `${c.email} ${now[c.email].tier}`).join(", ")}.`);
} catch (error) {
  // Undo in reverse order, so a failed step never leaves the seats half moved; a rollback that fails is reported.
  const undone = [];
  for (const { email, before } of made.reverse()) {
    try { await put(list, email, before); } catch (e) { undone.push(String(e.message || e).slice(0, 300)); }
  }
  console.error(String(error.message || error) + (made.length ? (undone.length ? ` Rollback failed: ${undone.join("; ")}.` : " Rolled back.") : ""));
  await done(1);
}
console.log(`Seats set: ${changes.map((c) => `${c.email} -> ${c.tier}`).join(", ")}.`);
await done(0);  // disconnects from CDP without closing the user's Chrome
