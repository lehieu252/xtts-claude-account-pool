// Receives rotation log events from the acct plugin and appends them to a Google Sheet.
// Setup: in the Sheet, Extensions > Apps Script, paste this file, then Deploy > New deployment >
// Web app (Execute as: Me, Who has access: Anyone). Put the /exec URL in LOG_URL of claude_acct.py.
const SHEET_NAME = "log";
const COLUMNS = ["ts", "machine", "host", "version", "hook", "event", "live", "premium", "target", "p5h", "p7d", "message"];
const HEADER = ["Thời gian", "Máy", "Host", "Version", "Hook", "Sự kiện", "Đang dùng", "Premium (config)", "Đích",
                "P 5h %", "P 7d %", "Message"];

function cell(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "number") return value;
  const text = String(value).slice(0, 1000);
  return /^[=+\-@]/.test(text) ? "'" + text : text;  // never let a value run as a formula
}

function doPost(e) {
  let events;
  try {
    const body = JSON.parse(e.postData.contents);
    events = Array.isArray(body.events) ? body.events : [];
  } catch (err) {
    return ContentService.createTextOutput("bad request");
  }
  events = events.filter(ev => ev && typeof ev.event === "string" && typeof ev.ts === "string").slice(0, 500);
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const book = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = book.getSheetByName(SHEET_NAME) || book.insertSheet(SHEET_NAME);
    if (sheet.getLastRow() === 0) {
      sheet.appendRow(HEADER);
      sheet.setFrozenRows(1);
    }
    if (events.length) {
      const rows = events.map(ev => COLUMNS.map(c => cell(ev[c])));
      sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, COLUMNS.length).setValues(rows);
    }
  } finally {
    lock.releaseLock();
  }
  return ContentService.createTextOutput("ok");
}
