import { chromium } from "playwright-core";
import { spawn } from "node:child_process";

// Usage: seat_manager.mjs <email>=<tier> ... applied in order, where <tier> is Premium, Standard or "No seat".
// The organization has one Premium and one Standard seat: free a seat before giving it to someone else.
const TIERS = { "Premium": /^\s*Premium seat/i, "Standard": /^\s*Standard seat/i, "No seat": /^\s*No seat/i };
const changes = process.argv.slice(2).filter((a) => a.includes("=")).map((a) => {
  const at = a.lastIndexOf("=");
  return { email: a.slice(0, at), tier: a.slice(at + 1) };
});
const membersUrl = process.env.CLAUDE_ACCT_MEMBERS_URL || "https://claude.ai/admin-settings/members";
const cdpUrl = process.env.CLAUDE_ACCT_CDP_URL || "http://127.0.0.1:9222";
const browserProfile = process.env.CLAUDE_ACCT_BROWSER_PROFILE;
const setupMode = process.argv[2] === "--setup";

if (!setupMode && (!changes.length || changes.some((c) => !c.email || !TIERS[c.tier]))) {
  throw new Error('Expected <email>=<tier> arguments, with tier Premium, Standard or "No seat".');
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
    // Keep rendering when the window is covered or the RDP session is disconnected.
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    membersUrl,
  ];
  let command, commandArgs;
  if (process.platform === "darwin") {
    command = "open";
    commandArgs = ["-gna", "Google Chrome", "--args", ...args];  // -g: stay in the background
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
const browser = await chromium.connectOverCDP(cdpUrl, { timeout: 10000, noDefaults: true });
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
  // Machine 2 may run with its RDP session disconnected or its window hidden: the page is then
  // "hidden" and never paints, so Playwright's actionability checks (visible, stable) never pass.
  // Everything below reads and clicks through the DOM, which works whether or not the page paints.
  await page.getByText(/Members/i).first().waitFor({ state: "attached", timeout: 15000 });
  if (/login|signin/i.test(page.url())) {
    throw new Error("The connected browser is not signed in to Claude. Sign in as an organization owner or admin and retry.");
  }
  // The sidebar includes a Members label before the member table finishes loading.
  // Wait for both target emails so subsequent row lookups don't race the table render.
  for (const { email } of changes) {
    await page.getByText(email, { exact: true }).first().waitFor({ state: "attached", timeout: 30000 });
  }

  const triggerFor = (email) => page.locator(`[role="combobox"][aria-label="Tier for ${email}"]`);
  const rowFor = async (email) => {
    const byRole = page.getByRole("row", { includeHidden: true }).filter({ hasText: email });
    if (await byRole.count()) return byRole.first();
    const text = page.getByText(email, { exact: true }).first();
    if (await text.count()) return text.locator("xpath=ancestor::tr[1]");
    throw new Error(`Could not find member ${email} on the Members page.`);
  };
  const tierControl = async (email) => {
    const trigger = triggerFor(email);
    if (await trigger.count()) return trigger.first();
    // Older page: the seat tier is the last combobox (or a Standard/Premium button) of the member's row.
    const row = await rowFor(email);
    const combos = row.locator('[role="combobox"]');
    if (await combos.count()) return combos.last();
    const button = row.locator("button").filter({ hasText: /^(Standard|Premium)$/i });
    if (await button.count()) return button.first();
    throw new Error(`Could not locate the seat tier control of ${email}.`);
  };
  const tierOf = async (email) => {
    const text = await (await tierControl(email)).evaluate((el) => el.textContent || "");
    // The trigger shows "Unassigned" for a member without a seat; the picker calls it "No seat assigned".
    const tier = text.match(/\b(Premium|Standard)\b/i) || (/no seat|unassigned/i.test(text) ? ["", "No seat"] : null);
    if (!tier) throw new Error(`Could not read the seat tier of ${email} ("${text.trim()}").`);
    return tier[1];
  };
  const chooseTier = async (control, tier) => {
    // Open the tier picker and click the "<tier> seat" option, all through the DOM.
    // Right after the page loads the picker ignores clicks until its handlers are attached, so
    // click again until it reports itself open (a click on an open picker would close it).
    const expanded = () => control.evaluate((el) => el.getAttribute("aria-expanded") === "true");
    const reportsState = await control.evaluate((el) => el.hasAttribute("aria-expanded"));
    for (let click = 0; click < (reportsState ? 10 : 1) && !(await expanded()); click++) {
      await control.evaluate((el) => el.click());
      for (let wait = 0; wait < 8 && !(await expanded()); wait++) await page.waitForTimeout(250);
    }
    let picked = false;
    for (let attempt = 0; attempt < 20 && !picked; attempt++) {
      await page.waitForTimeout(250);
      picked = await control.evaluate((el, pattern) => {
        const popup = document.getElementById(el.getAttribute("aria-controls") || "") || document;
        const option = [...popup.querySelectorAll('[role="option"]')]
          .find((o) => new RegExp(pattern, "i").test(o.textContent || ""));
        if (!option) return false;
        option.scrollIntoView({ block: "nearest" });
        option.click();
        return true;
      }, TIERS[tier].source);
    }
    if (!picked) {
      await page.keyboard.press("Escape").catch(() => {});
      throw new Error(`The seat picker did not offer "${tier}".`);
    }
  };
  const setTier = async (email, tier) => {
    if ((await tierOf(email)) === tier) return;
    await chooseTier(await tierControl(email), tier);
    let actual;
    for (let attempt = 0; attempt < 40; attempt++) {
      actual = await tierOf(email);
      if (actual === tier) return;
      await page.waitForTimeout(500);
    }
    throw new Error(`Seat tier for ${email} did not become ${tier} (currently ${actual}).`);
  };

  const before = [];
  for (const { email } of changes) before.push({ email, tier: await tierOf(email) });
  const done = [];
  try {
    for (const change of changes) {
      done.push(change);
      await setTier(change.email, change.tier);
    }
  } catch (error) {
    // Best-effort rollback, in reverse order, so a failed step never leaves the seats half moved.
    for (const { email } of done.reverse()) {
      const original = before.find((b) => b.email === email);
      try { await setTier(email, original.tier); } catch {}
    }
    throw error;
  }
  console.log(`Seats set: ${changes.map((c) => `${c.email} → ${c.tier}`).join(", ")}.`);
process.exit(0); // Disconnect from CDP without closing the user's Chrome instance.
