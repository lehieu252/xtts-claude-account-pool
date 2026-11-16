import { chromium } from "playwright-core";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

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
// seat_manager.mjs --read <email> ...: print {"<email>": "Premium" | "Standard" | "No seat"} and change nothing.
const readEmails = process.argv[2] === "--read" ? process.argv.slice(3) : null;

if (!setupMode && !readEmails?.length && (!changes.length || changes.some((c) => !c.email || !TIERS[c.tier]))) {
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
  // The sidebar includes a Members label before the member table finishes loading: wait for the table.
  const tableLoaded = () => page.locator('[role="combobox"][aria-label^="Tier for"]').first()
    .waitFor({ state: "attached", timeout: 30000 });
  await tableLoaded();

  const triggerFor = (email) => page.locator(`[role="combobox"][aria-label="Tier for ${email}"]`);
  // The table shows 15 members a page: a member on another page is found with the "Search members" box,
  // or else by turning the pages. All through the DOM, like the clicks (the page may be hidden).
  // Present, and still present a moment later: the search results are re-rendered once more after typing.
  const settled = async (email) => {
    for (let wait = 0; wait < 60; wait++) {
      if (await triggerFor(email).count()) {
        await page.waitForTimeout(1200);
        if (await triggerFor(email).count()) return true;
      } else {
        await page.waitForTimeout(250);
      }
    }
    return false;
  };
  const findMember = async (email) => {
    if (await triggerFor(email).count()) return;
    const searched = await page.evaluate((value) => {
      const input = document.querySelector('input[name="members-search"], input[aria-label="Search members"]');
      if (!input) return false;
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }, email);
    if (searched && await settled(email)) return;
    for (let turn = 0; turn < 20; turn++) {
      const next = page.locator('button[aria-label="Next page"]');
      if (!(await next.count()) || await next.first().evaluate((b) => b.disabled || b.getAttribute("aria-disabled") === "true")) break;
      await next.first().evaluate((b) => b.click());
      await page.waitForTimeout(1500);
      if (await triggerFor(email).count() && await settled(email)) return;
    }
  };
  const rowFor = async (email) => {
    const byRole = page.getByRole("row", { includeHidden: true }).filter({ hasText: email });
    if (await byRole.count()) return byRole.first();
    const text = page.getByText(email, { exact: true }).first();
    if (await text.count()) return text.locator("xpath=ancestor::tr[1]");
    throw new Error(`Could not find member ${email} on the Members page.`);
  };
  const tierControl = async (email) => {
    await findMember(email);
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
  // What the page shows besides the table: toasts, alerts, dialogs, and the "N (M available)" seat count.
  // Kept right after each click and saved with a screenshot when a change does not take, to see why.
  const notes = () => page.evaluate(() => {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 200);
    const shown = [...document.querySelectorAll('[role="alert"],[role="alertdialog"],[role="dialog"],[role="status"],[data-sonner-toast]')]
      .map((n) => clean(n.textContent)).filter((t) => t && !/^Showing \d/.test(t) && !/^No seat assigned/.test(t));
    const seats = ((document.body ? document.body.innerText : "").match(/([^\n]*)\n\s*Total seats/) || [])[1];
    return { shown: [...new Set(shown)].slice(0, 6), seats: seats ? clean(seats) : null };
  }).catch((error) => ({ shown: [], seats: null, error: String(error).slice(0, 200) }));
  let afterClick = null;
  const storeDir = process.env.CLAUDE_ACCT_HOME || path.join(os.homedir(), ".claude-acct");
  const describe = (n) => !n ? "-" : [n.seats && `seats ${n.seats}`, n.shown.length && `shown "${n.shown.join(" | ")}"`,
    n.error && `unreadable (${n.error})`].filter(Boolean).join(", ") || "nothing shown";
  const capture = async (label) => {
    const now = await notes();
    let saved = "";
    try {
      const dir = path.join(storeDir, "seat-fail");
      fs.mkdirSync(dir, { recursive: true });
      const base = path.join(dir, new Date().toISOString().replace(/[:.]/g, "-"));
      fs.writeFileSync(base + ".json", JSON.stringify({ at: new Date().toISOString(), label, url: page.url(), afterClick, now }, null, 1));
      // A hidden page (RDP disconnected) may never paint: the screenshot is a bonus, never a reason to wait.
      await page.screenshot({ path: base + ".png", fullPage: true, timeout: 10000 }).catch(() => {});
      for (const old of fs.readdirSync(dir).sort().slice(0, -40)) fs.rmSync(path.join(dir, old), { force: true });
      saved = ` Saved ${base}.json (.png).`;
    } catch {}
    return ` Page after the click: ${describe(afterClick)}; now: ${describe(now)}.${saved}`;
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
    for (let attempt = 0; attempt < 40 && !picked; attempt++) {
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
    await page.waitForTimeout(1500);  // a toast or dialog answering the click shows up within a moment
    afterClick = await notes();
  };
  const reload = async () => {
    await page.goto(membersUrl, { waitUntil: "domcontentloaded", timeout: 30000 });
    await tableLoaded();
    await page.waitForTimeout(2000);  // let the page attach its handlers before the first click
  };
  // Click the option; false when the tier turned out to be set already. The picker sometimes does not open,
  // or opens empty, for a while: reload the page and try again.
  const pick = async (email, tier) => {
    for (let attempt = 1; ; attempt++) {
      try {
        await chooseTier(await tierControl(email), tier);
        return true;
      } catch (error) {
        if (attempt >= 3 || !/did not offer/.test(error.message)) throw error;
        await reload();
        if ((await tierOf(email)) === tier) return false;
      }
    }
  };
  // null once the tier is set, else the tier still shown after `rounds` checks half a second apart.
  const waitTier = async (email, tier, rounds) => {
    let actual;
    for (let attempt = 0; attempt < rounds; attempt++) {
      actual = await tierOf(email);
      if (actual === tier) return null;
      await page.waitForTimeout(500);
    }
    return actual;
  };
  const CLICKS = 2;
  const setTier = async (email, tier) => {
    if ((await tierOf(email)) === tier) return;
    // Since 2026-11-14 claude.ai sometimes leaves a Premium seat given right after it was freed on "No seat", or
    // applies it minutes later. Each click is checked for 20 s, then once more after a reload (the table can lag);
    // a second click follows only if the tier is still not set. About 80 s in all before giving up.
    let actual;
    for (let click = 1; click <= CLICKS; click++) {
      if (!(await pick(email, tier))) return;
      if (!(actual = await waitTier(email, tier, 40))) return;
      await reload();
      if (!(actual = await waitTier(email, tier, 20))) return;
    }
    throw new Error(`Seat tier for ${email} did not become ${tier} (currently ${actual}) after ${CLICKS} clicks.`
      + await capture(`${email} -> ${tier}`));
  };

  if (readEmails) {
    const tiers = {};
    for (const email of readEmails) tiers[email] = await tierOf(email);
    console.log(JSON.stringify(tiers));
    process.exit(0);
  }

  const before = [];
  for (const { email } of changes) before.push({ email, tier: await tierOf(email) });
  const done = [];
  try {
    for (const change of changes) {
      done.push(change);
      await setTier(change.email, change.tier);
    }
  } catch (error) {
    // A change claude.ai applied late is a success: read every member again before undoing anything.
    try {
      await reload();
      let applied = true;
      for (const change of changes) if ((await tierOf(change.email)) !== change.tier) { applied = false; break; }
      if (applied) {
        console.log(`Seats set: ${changes.map((c) => `${c.email} -> ${c.tier}`).join(", ")} (applied late).`);
        process.exit(0);
      }
    } catch {}
    // Rollback, in reverse order, so a failed step never leaves the seats half moved. A rollback that fails too
    // is reported (machine 2 then reads the seats on claude.ai instead of trusting the old ones).
    const undone = [];
    for (const { email } of done.reverse()) {
      const original = before.find((b) => b.email === email);
      try { await setTier(email, original.tier); }
      catch (e) { undone.push(`${email} -> ${original.tier}: ${String(e.message || e).split("\n")[0].slice(0, 300)}`); }
    }
    // The message alone, without a stack trace, so the log and the dashboard show the reason in full.
    console.error(error.message + (undone.length ? ` Rollback failed: ${undone.join("; ")}.` : " Rolled back."));
    process.exit(1);
  }
  console.log(`Seats set: ${changes.map((c) => `${c.email} -> ${c.tier}`).join(", ")}.`);
process.exit(0); // Disconnect from CDP without closing the user's Chrome instance.
