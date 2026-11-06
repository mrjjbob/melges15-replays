/**
 * Fleet uploader: gets .vkx files into the site's logs/ folder without anyone needing GitHub.
 *
 *  1. Email:  share the session from Vakaros Connect to this Gmail address (Share → Mail).
 *             Every 5 minutes new mail is checked, .vkx attachments are added to logs/, and the
 *             sender gets a short reply.
 *  2. Web page: the upload page (doGet) for people who would rather pick a file.
 *
 * Runs on a Google account set up for the fleet. Settings live in Project Settings → Script
 * properties (see uploader/README.md):
 *   GITHUB_TOKEN    fine-grained token, this repository only, Contents: read and write
 *   REPO            mrjjbob/melges15-replays
 *   BRANCH          main
 *   SITE_URL        https://mrjjbob.github.io/melges15-replays/
 *   UPLOAD_PASSWORD the fleet password for the web page (leave empty for none)
 *   MAIL_TO         optional: only handle mail sent to this address, e.g. you+melges15@gmail.com,
 *                   so the script can run on your own Gmail and leave the rest of it alone
 */

const MAX_BYTES = 40 * 1024 * 1024;

function prop_(k, dflt) {
  const v = PropertiesService.getScriptProperties().getProperty(k);
  return v == null || v === '' ? dflt : v;
}

// ---------------------------------------------------------------------------------- GitHub ---

function gh_(method, path, body) {
  const res = UrlFetchApp.fetch('https://api.github.com/repos/' + prop_('REPO') + path, {
    method: method,
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + prop_('GITHUB_TOKEN'),
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: body ? JSON.stringify(body) : undefined,
    muteHttpExceptions: true,
  });
  return { code: res.getResponseCode(), json: (() => { try { return JSON.parse(res.getContentText()); } catch (e) { return null; } })() };
}

function fleet_() {
  const cache = CacheService.getScriptCache();
  const hit = cache.get('fleet');
  if (hit) return JSON.parse(hit);
  const r = gh_('get', '/contents/fleet.json?ref=' + prop_('BRANCH', 'main'));
  let boats = [];
  if (r.code === 200) {
    boats = JSON.parse(Utilities.newBlob(Utilities.base64Decode(r.json.content)).getDataAsString()).boats || [];
  }
  cache.put('fleet', JSON.stringify(boats), 600);
  return boats;
}

function clean_(s) {
  return String(s || '').replace(/[^A-Za-z0-9._-]+/g, '_').replace(/_+/g, '_').replace(/^_|_$/g, '');
}

/** Add one file to logs/. Returns 'added', 'already there' or an error message. */
function addLog_(bytes, filename, sail) {
  if (!bytes || bytes.length < 64) return 'that file is empty';
  if (bytes.length > MAX_BYTES) return 'that file is too big (' + Math.round(bytes.length / 1048576) + ' MB)';
  if ((bytes[0] & 0xff) !== 0xff) return 'that is not a Vakaros .vkx file';
  let name = clean_(filename.replace(/\.vkx$/i, ''));
  const boat = sail && fleet_().find(b => String(b.sail) === String(sail));
  // the sail number up front is what the site matches boats on
  if (boat && name.split(/[_-]/).indexOf(String(boat.sail)) < 0) name = clean_(boat.sail + '_' + boat.name) + '__' + name;
  const path = 'logs/' + name + '.vkx';
  const exists = gh_('get', '/contents/' + encodeURI(path) + '?ref=' + prop_('BRANCH', 'main'));
  if (exists.code === 200) return 'already there';
  const put = gh_('put', '/contents/' + encodeURI(path), {
    message: 'Upload ' + name + '.vkx' + (boat ? ' (' + boat.sail + ' ' + boat.name + ')' : ''),
    content: Utilities.base64Encode(bytes),
    branch: prop_('BRANCH', 'main'),
  });
  if (put.code === 201 || put.code === 200) return 'added';
  return 'GitHub said ' + put.code + (put.json && put.json.message ? ': ' + put.json.message : '');
}

/** A sail number from the fleet mentioned in some text (subject, body). */
function sailIn_(text) {
  const words = String(text || '').match(/\d{2,4}/g) || [];
  const sails = fleet_().map(b => String(b.sail));
  return words.find(w => sails.indexOf(w) >= 0) || null;
}

// ----------------------------------------------------------------------------------- Email ---

function checkMail() {
  const me = Session.getEffectiveUser().getEmail().toLowerCase();
  const only = prop_('MAIL_TO', '');
  const threads = GmailApp.search((only ? 'to:' + only : 'in:inbox') + ' is:unread newer_than:7d', 0, 20);
  threads.forEach(th => {
    th.getMessages().forEach(msg => {
      if (!msg.isUnread() || msg.isInTrash()) return;
      msg.markRead();
      if (msg.getFrom().toLowerCase().indexOf(me) >= 0) return;          // our own replies
      const sail = sailIn_(msg.getSubject()) || sailIn_(msg.getPlainBody().slice(0, 500));
      const lines = [];
      msg.getAttachments({ includeInlineImages: false }).forEach(att => {
        const n = att.getName() || 'session.vkx';
        if (!/\.vkx$/i.test(n)) return;
        lines.push(n + ': ' + addLog_(att.getBytes(), n, sail));
      });
      const site = prop_('SITE_URL', '');
      if (lines.length) {
        const ok = lines.some(l => /: added$/.test(l));
        msg.reply((ok ? 'Thanks, got it.\n\n' : 'Thanks.\n\n') + lines.join('\n') +
          (ok ? '\n\nYour races will be on the site in about 3 minutes:\n' + site : '') +
          (sail ? '' : '\n\nTip: put your sail number in the subject so the file is always matched to your boat.'));
      } else {
        msg.reply('Thanks, but there was no .vkx file attached.\n\nIn Vakaros Connect, open the session, ' +
          'tap Share/Export, choose the .vkx file and send it with Mail to this address.\n\n' + site);
      }
    });
  });
}

/** Run once from the editor: checks mail every 5 minutes from now on. */
function setup() {
  ScriptApp.getProjectTriggers().forEach(t => ScriptApp.deleteTrigger(t));
  ScriptApp.newTrigger('checkMail').timeBased().everyMinutes(5).create();
  fleet_();
  Logger.log('Mail is checked every 5 minutes. Fleet boats: ' + fleet_().length);
}

// -------------------------------------------------------------------------------- Web page ---

function doGet() {
  const t = HtmlService.createTemplateFromFile('Upload');
  t.boats = fleet_();
  t.site = prop_('SITE_URL', '');
  t.email = prop_('MAIL_TO', '') || Session.getEffectiveUser().getEmail();
  t.needPassword = !!prop_('UPLOAD_PASSWORD', '');
  return t.evaluate()
    .setTitle('Upload your race')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

/** Called from the page, one file at a time. */
function uploadFromPage(f) {
  const pw = prop_('UPLOAD_PASSWORD', '');
  if (pw && String(f.password || '').trim().toLowerCase() !== pw.trim().toLowerCase()) {
    return { ok: false, msg: 'Wrong fleet password.' };
  }
  const result = addLog_(Utilities.base64Decode(f.data), f.name, f.sail);
  return { ok: result === 'added' || result === 'already there', msg: result };
}
