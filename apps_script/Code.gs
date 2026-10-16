/**
 * Blaze speed-to-lead: append one lead row per POST.
 *
 * Setup: Project Settings > Script Properties > add SHEETS_TOKEN (same value as in .env).
 * Deploy: Deploy > New deployment > Web app > Execute as: Me > Who has access: Anyone.
 *
 * Request body (JSON): {"token": "...", "row": {"received_at": "...", ...}}
 * Response (JSON):     {"ok": true, "row": <row number>} or {"ok": false, "error": "..."}
 */

var HEADERS = [
  // Lead
  'received_at', 'leadgen_id', 'name', 'email', 'phone_e164', 'company', 'doors', 'message',
  // Scoring
  'fit_score', 'reason', 'rule_overrides',
  // Drafts
  'sms_draft', 'email_subject', 'email_body',
  // Run info
  'status', 'latency_ms', 'model'
];

var SHEET_NAME = 'Leads';

function doPost(e) {
  try {
    var body = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    var expected = PropertiesService.getScriptProperties().getProperty('SHEETS_TOKEN');
    if (!expected || body.token !== expected) {
      return json_({ ok: false, error: 'unauthorized' });
    }
    var row = body.row || {};

    var lock = LockService.getScriptLock();
    lock.waitLock(10000);
    try {
      var ss = SpreadsheetApp.getActiveSpreadsheet();
      var sheet = ss.getSheetByName(SHEET_NAME) || ss.insertSheet(SHEET_NAME);
      if (sheet.getLastRow() === 0) {
        sheet.appendRow(HEADERS);
        sheet.setFrozenRows(1);
        sheet.getRange(1, 1, 1, HEADERS.length).setFontWeight('bold');
      }
      var values = HEADERS.map(function (h) {
        var v = row[h];
        return v === null || v === undefined ? '' : v;
      });
      sheet.appendRow(values);
      return json_({ ok: true, row: sheet.getLastRow() });
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

function doGet() {
  return json_({ ok: true, service: 'blaze-speed-to-lead sheet logger' });
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
