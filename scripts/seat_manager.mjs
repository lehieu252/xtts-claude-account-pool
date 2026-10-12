import { chromium } from "playwright-core";
import { spawn } from "node:child_process";

const [oldEmail, newEmail] = process.argv.slice(2);
const membersUrl = process.env.CLAUDE_ACCT_MEMBERS_URL || "https://claude.ai/admin-settings/members";
const cdpUrl = process.env.CLAUDE_ACCT_CDP_URL || "http://127.0.0.1:9222";
const browserProfile = process.env.CLAUDE_ACCT_BROWSER_PROFILE;
const setupMode = process.argv[2] === "--setup";

if (!setupMode && (!oldEmail || !newEmail || oldEmail.toLowerCase() === newEmail.toLowerCase())) {
  throw new Error("Expected two different account emails.");
}
const members = new URL(membersUrl);
if (members.protocol !== "https:" || members.hostname !== "claude.ai") {
  throw new Error("Members URL must be an HTTPS claude.ai page.");
}
const cdp = new URL(cdpUrl);
if (!((cdp.hostname === "127.0.0.1" || cdp.hostname === "localhost" || cdp.hostname === "::1") &&
      (cdp.protocol === "http:" || cdp.protocol === "https:"))) {
  throw new Error("Playwright CDP must point to localhost.");
}

const versionUrl = new URL("/json/version", cdpUrl).toString();
const hasCdp = async () => {
  try { return (await fetch(versionUrl, { signal: AbortSignal.timeout(700) })).ok; }
  catch { return false; }
};
const startRegularChrome = async () => {
  if (!browserProfile) throw new Error(`No Chrome CDP endpoint at ${cdpUrl}.`);
  const args = [
    `--user-data-dir=${browserProfile}`,
    `--remote-debugging-port=${cdp.port || "9222"}`,
    "--no-first-run",
    "--new-window",
    membersUrl,
  ];
  let command, commandArgs;
  if (process.platform === "darwin") {
    command = "open";
    commandArgs = ["-na", "Google Chrome", "--args", ...args];
  } else if (process.platform === "win32") {
    command = process.env.CLAUDE_ACCT_CHROME || "chrome.exe";
    commandArgs = args;
  } else {
    command = process.env.CLAUDE_ACCT_CHROME || "google-chrome";
    commandArgs = args;
  }
  const child = spawn(command, commandArgs, { detached: true, stdio: "ignore" });
  child.on("error", () => {});
  child.unref();
  for (let attempt = 0; attempt < 50; attempt++) {
    if (await hasCdp()) return;
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error("Could not connect to Chrome. Close any other window using the automation profile, then run /acct:auto browser again.");
};

if (!(await hasCdp())) await startRegularChrome();
const browser = await chromium.connectOverCDP(cdpUrl, { timeout: 10000 });
const context = browser.contexts()[0];
  if (!context) throw new Error("No browser context found at the CDP endpoint.");
  let page = context.pages().find((p) => p.url().startsWith("https://claude.ai/"));
  if (!page) page = await context.newPage();
  await page.goto(membersUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
  if (setupMode) {
    console.log("Chrome is using the saved automation profile. Sign in as an organization owner/admin if needed, then return here; keep the browser open for automatic seat changes.");
    await new Promise((resolve) => browser.on("disconnected", resolve));
    process.exit(0);
  }
  await page.getByText(/Members/i).first().waitFor({ timeout: 15000 });
  if (/login|signin/i.test(page.url())) {
    throw new Error("The connected browser is not signed in to Claude. Sign in as an organization owner or admin and retry.");
  }

  const rowFor = async (email) => {
    const byRole = page.getByRole("row").filter({ hasText: email });
    if (await byRole.count()) return byRole.first();
    const text = page.getByText(email, { exact: true }).first();
    if (await text.count()) return text.locator("xpath=ancestor::tr[1]");
    throw new Error(`Could not find member ${email} on the Members page.`);
  };
  const tierOf = async (row) => {
    const buttons = row.getByRole("button", { name: /^(Standard|Premium)$/i });
    if (await buttons.count()) return (await buttons.first().innerText()).trim();
    const selects = row.getByRole("combobox");
    if (await selects.count()) return (await selects.first().innerText()).trim();
    throw new Error("Could not locate a seat tier control for a member row.");
  };
  const setTier = async (email, tier) => {
    let row = await rowFor(email);
    const current = await tierOf(row);
    if (new RegExp(`^${tier}$`, "i").test(current)) return;
    const combo = row.getByRole("combobox");
    const control = await combo.count()
      ? combo.last()
      : row.getByRole("button", { name: /^(Standard|Premium)$/i }).first();
    await control.click();
    const option = page.getByText(new RegExp(`^${tier} seat\\b`, "i")).last();
    await option.click();
    await page.waitForTimeout(700);
    row = await rowFor(email);
    const actual = await tierOf(row);
    if (!new RegExp(`^${tier}$`, "i").test(actual)) {
      throw new Error(`Seat tier for ${email} did not become ${tier} (currently ${actual}).`);
    }
  };

  const oldRow = await rowFor(oldEmail);
  const newRow = await rowFor(newEmail);
  const oldTier = await tierOf(oldRow);
  const newTier = await tierOf(newRow);
  if (!/^Premium$/i.test(oldTier)) throw new Error(`${oldEmail} is not currently Premium (${oldTier}).`);
  if (!/^Standard$/i.test(newTier)) throw new Error(`${newEmail} must be Standard before rotation (${newTier}).`);

  await setTier(oldEmail, "Standard");
  try {
    await setTier(newEmail, "Premium");
  } catch (error) {
    // Best-effort rollback prevents a failed promotion from leaving the pool without Premium.
    try { await setTier(newEmail, "Standard"); } catch {}
    try { await setTier(oldEmail, "Premium"); } catch {}
    throw error;
  }
  console.log(`Premium seat transferred from ${oldEmail} to ${newEmail}.`);
process.exit(0); // Disconnect from CDP without closing the user's Chrome instance.
