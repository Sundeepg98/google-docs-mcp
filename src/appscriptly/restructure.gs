/**
 * google-docs-mcp / Wave E restructure script
 *
 * Web App entry point. Receives a JSON POST with {docId, splitTree} and moves
 * body content from the primary tab into pre-created child tabs (the REST API
 * creates the empty tab shells before this runs - Apps Script cannot create
 * tabs, but it CAN do Element.copy() + Body.appendXxx(element) which the
 * REST API cannot.)
 *
 * Algorithm:
 *  1. Snapshot the source body's children (so subsequent mutation doesn't
 *     shift indices we still need to read).
 *  2. Flatten splitTree depth-first and pair each node with a pre-created tab
 *     in the same depth-first order; tabs[0] stays as the primary "Full
 *     document" tab and is left untouched in case a non-covered prefix exists.
 *  3. For each target tab, clear its placeholder paragraph, then for each
 *     [start, end] range in the node, copy() each source child and append by
 *     element type.
 *  4. After all copies are recorded, remove the moved children from the
 *     source body. Bodies must keep >=1 child, so we leave a final blank
 *     paragraph if everything was moved.
 *
 * REQUEST AUTHENTICATION (HMAC verify-path, query-param transport).
 * The Web App is deployed access=ANYONE_ANONYMOUS (the MCP server posts to
 * /exec with no Google sign-in), so URL secrecy alone is NOT the access
 * control any more: doPost authenticates every request with a per-user
 * HMAC-SHA256 signature before touching any document. The setup pipeline
 * (setup_apps_script.py) generates a 64-hex-char per-user key, persists it
 * (user_store / local config), and templates it into the placeholder below
 * before pushing this source to Apps Script. The MCP server signs each POST
 * with the SAME key (docx_import._call_webapp), sending the signature as
 * query params on the /exec URL: ?mcp_ts=<unix seconds>&mcp_sig=<hex>.
 * Query params are the ONLY transport available: the Apps Script runtime
 * never delivers HTTP request headers to doPost(e); the event carries only
 * e.parameter / e.parameters / e.postData / e.queryString. A request with a
 * missing / stale / mismatched signature is rejected with
 * {success:false, stage:'auth'} and never mutates a doc.
 *
 * The two sentinels below are REPLACED at deploy time by setup_apps_script.py
 * (string substitution on this file's text). If the substitution did not run
 * (key empty / template marker intact), HMAC is treated as NOT configured and
 * doPost FAILS CLOSED (see _verifyHmac). The markers are intentionally exact
 * literals so the Python side can find-and-replace them deterministically.
 */

// Replaced at deploy time with the per-user 64-hex-char HMAC key.
var MCP_HMAC_KEY = '__MCP_HMAC_KEY__';
// Replaced at deploy time with 'true' once a key is provisioned. Left as the
// literal marker otherwise — _verifyHmac treats anything but 'true' as
// "not configured" and fails closed.
var MCP_HMAC_REQUIRED = '__MCP_HMAC_REQUIRED__';
// Reject requests whose mcp_ts is more than this many seconds away
// from the script's clock (replay / stale-capture window). 300s = 5 min,
// generous enough for clock skew + a slow network without leaving a wide
// replay window.
var MCP_HMAC_MAX_SKEW_SECONDS = 300;

function doPost(e) {
  // 1. Authenticate BEFORE parsing/acting. A failed check returns stage:'auth'
  //    and never reaches restructureToTabs, so an unsigned/forged request
  //    cannot mutate any document.
  var auth = _verifyHmac(e);
  if (!auth.ok) {
    return _json({success: false, stage: 'auth', error: auth.error});
  }

  var payload;
  try {
    payload = JSON.parse(e.postData.contents);
  } catch (err) {
    return _json({success: false, stage: 'parse_request', error: String(err)});
  }
  try {
    var result = restructureToTabs(payload.docId, payload.splitTree || []);
    return _json({success: true, stage: 'complete', tabs: result.tabs,
                  movedChildren: result.movedChildren, warnings: result.warnings});
  } catch (err) {
    return _json({success: false, stage: err.stage || 'unknown',
                  error: err.message || String(err), trace: err.stack || ''});
  }
}

/**
 * Verify the per-request HMAC signature.
 *
 * Scheme (must match docx_import._call_webapp exactly):
 *   signature = lowercase_hex( HMAC_SHA256(key, timestamp + "." + rawBody) )
 * sent as query params on the /exec URL: mcp_sig carries the signature,
 * mcp_ts the unix-seconds timestamp. The Apps Script runtime surfaces the
 * query string to doPost as e.parameter and NEVER delivers HTTP request
 * headers, so the query string is the only channel this verify can read.
 * Signing the timestamp TOGETHER with the body binds the two so a captured
 * (body, signature) pair can't be replayed with a fresh timestamp, and a
 * stale timestamp is rejected by the skew window; the signature is both
 * time-bound and body-bound, which caps the replay value of a logged URL.
 *
 * FAILS CLOSED: if no key was templated in (deploy-time substitution didn't
 * run), every request is rejected. We never silently accept unsigned
 * traffic. Returns {ok:true} or {ok:false, error:<reason>}.
 */
function _verifyHmac(e) {
  // Not configured: fail closed. (MCP_HMAC_REQUIRED stays as its literal
  // marker, or the key is empty / still the template marker, when the deploy
  // substitution didn't run.)
  if (MCP_HMAC_REQUIRED !== 'true' ||
      !MCP_HMAC_KEY || MCP_HMAC_KEY === ('__MCP_HMAC' + '_KEY__')) {
    return {ok: false, error: 'server HMAC key not configured (re-run install/setup)'};
  }

  var params = (e && e.parameter) || {};
  var sig = params.mcp_sig;
  var tsRaw = params.mcp_ts;
  if (!sig || !tsRaw) {
    return {ok: false, error: 'missing mcp_sig / mcp_ts query parameter'};
  }

  var ts = parseInt(tsRaw, 10);
  if (isNaN(ts)) {
    return {ok: false, error: 'malformed mcp_ts'};
  }
  var now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > MCP_HMAC_MAX_SKEW_SECONDS) {
    return {ok: false, error: 'stale or future timestamp (replay window exceeded)'};
  }

  var body = (e && e.postData && e.postData.contents) || '';
  var expected = _computeHmacHex(MCP_HMAC_KEY, tsRaw + '.' + body);
  if (!_constantTimeEquals(expected, String(sig).toLowerCase())) {
    return {ok: false, error: 'signature mismatch'};
  }
  return {ok: true};
}

/** Compute lowercase-hex HMAC-SHA256 over ``message`` with ``key``. */
function _computeHmacHex(key, message) {
  var raw = Utilities.computeHmacSha256Signature(message, key);
  var hex = '';
  for (var i = 0; i < raw.length; i++) {
    // Bytes are signed in Apps Script (-128..127); mask to 0..255.
    var b = (raw[i] + 256) % 256;
    var h = b.toString(16);
    if (h.length === 1) h = '0' + h;
    hex += h;
  }
  return hex;
}

/**
 * Constant-time string compare. Avoids the early-exit timing leak of ``===``
 * so an attacker can't recover the expected signature byte-by-byte from
 * response timing. Length is compared via accumulation too (the lengths are
 * both fixed 64-hex here, but stay defensive).
 */
function _constantTimeEquals(a, b) {
  a = String(a);
  b = String(b);
  var diff = a.length ^ b.length;
  var max = Math.max(a.length, b.length);
  for (var i = 0; i < max; i++) {
    diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  }
  return diff === 0;
}

function doGet() {
  // Quick health-check so users can verify the deployment URL in a browser.
  return _json({ok: true, service: 'google-docs-mcp restructure', version: '1'});
}

function _json(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function _staged(stage, message) {
  var err = new Error(message);
  err.stage = stage;
  return err;
}

function restructureToTabs(docId, splitTree) {
  if (!docId) throw _staged('parse_request', 'Missing docId');

  var doc;
  try { doc = DocumentApp.openById(docId); }
  catch (err) { throw _staged('open_doc', 'openById failed: ' + err); }

  var tabs = doc.getTabs();          // flat list of TOP-LEVEL tabs only
  var flatAll = _flattenTabs(tabs);  // depth-first incl. nested children
  if (flatAll.length < 1) throw _staged('enumerate_tabs', 'Document has no tabs');

  if (!splitTree.length) {
    return {tabs: _describeTabs(flatAll), movedChildren: 0, warnings: ['no_splits']};
  }

  var flatSplits = _flattenSplits(splitTree);
  // tabs[0] is the primary; flatSplits maps 1:1 onto flatAll.slice(1).
  var targetTabs = flatAll.slice(1);
  if (targetTabs.length !== flatSplits.length) {
    throw _staged('enumerate_tabs',
      'Tab/split count mismatch: ' + targetTabs.length + ' tabs vs ' +
      flatSplits.length + ' splits. Did REST batchUpdate fail partway?');
  }

  // Snapshot source children. Body.getChild(i) returns live references but
  // we want a stable list we can iterate without index-shift surprises.
  var sourceBody = flatAll[0].asDocumentTab().getBody();
  var n = sourceBody.getNumChildren();
  var sourceChildren = [];
  for (var i = 0; i < n; i++) sourceChildren.push(sourceBody.getChild(i));

  var warnings = [];
  var movedSet = {};   // child index -> true once moved
  var movedCount = 0;

  // Copy phase. We do NOT mutate the source body yet; only after all targets
  // are populated do we remove the moved children. This way a mid-flight
  // failure leaves the source intact (auditable rollback by hand).
  for (var s = 0; s < flatSplits.length; s++) {
    var node = flatSplits[s];
    var targetBody = targetTabs[s].asDocumentTab().getBody();
    _clearPlaceholder(targetBody);

    var ranges = node.ranges || [];
    for (var r = 0; r < ranges.length; r++) {
      var lo = ranges[r][0], hi = ranges[r][1];
      if (lo < 0 || hi >= n || lo > hi) {
        warnings.push('range_out_of_bounds:' + lo + '-' + hi + ' (max=' + (n-1) + ')');
        continue;
      }
      for (var c = lo; c <= hi; c++) {
        try {
          _appendCopy(targetBody, sourceChildren[c], warnings);
          movedSet[c] = true;
          movedCount++;
        } catch (err) {
          throw _staged('copy_children',
            'Failed copying child ' + c + ' of type ' +
            sourceChildren[c].getType() + ': ' + err);
        }
      }
    }
    // Empty body looks weird - append a blank paragraph if nothing landed.
    if (targetBody.getNumChildren() === 0) targetBody.appendParagraph('');
  }

  // Cleanup phase: remove moved children from source body, in reverse so
  // earlier indices remain valid for later removals.
  try {
    for (var k = sourceChildren.length - 1; k >= 0; k--) {
      if (!movedSet[k]) continue;
      if (sourceBody.getNumChildren() <= 1) break;  // bodies need >= 1 child
      try { sourceBody.removeChild(sourceChildren[k]); }
      catch (err) { warnings.push('remove_failed:' + k + ':' + err); }
    }
  } catch (err) {
    throw _staged('cleanup_source', err.message || String(err));
  }

  return {tabs: _describeTabs(flatAll), movedChildren: movedCount, warnings: warnings};
}

function _appendCopy(targetBody, original, warnings) {
  var T = DocumentApp.ElementType;
  var copy = original.copy();
  switch (original.getType()) {
    case T.PARAGRAPH:        targetBody.appendParagraph(copy); break;
    case T.TABLE:            targetBody.appendTable(copy); break;
    case T.LIST_ITEM:        targetBody.appendListItem(copy); break;
    case T.PAGE_BREAK:       targetBody.appendPageBreak(copy); break;
    case T.HORIZONTAL_RULE:  targetBody.appendHorizontalRule(); break;  // no-arg variant
    case T.TABLE_OF_CONTENTS:
      // TOC is positional and references the source doc structure; copying
      // produces a stale TOC. Emit a placeholder so the user knows.
      targetBody.appendParagraph('[table of contents - regenerate via Insert > Table of contents]');
      warnings.push('toc_skipped');
      break;
    case T.UNSUPPORTED:
      targetBody.appendParagraph('[unsupported element omitted by Apps Script]');
      warnings.push('unsupported_element');
      break;
    default:
      // Anything else (e.g. exotic inline-only) - try as paragraph fallback.
      targetBody.appendParagraph('[unhandled element type: ' + original.getType() + ']');
      warnings.push('unhandled:' + original.getType());
  }
}

function _clearPlaceholder(body) {
  // New tabs come with one empty paragraph. Wipe everything; the caller
  // guarantees we'll append at least one element or a blank fallback.
  while (body.getNumChildren() > 0) {
    if (body.getNumChildren() === 1) {
      // Last child can't be removed - clear its content instead.
      var last = body.getChild(0);
      if (last.getType() === DocumentApp.ElementType.PARAGRAPH && last.getText() === '') return;
    }
    try { body.removeChild(body.getChild(0)); } catch (e) { return; }
  }
}

function _flattenTabs(tabs) {
  var out = [];
  for (var i = 0; i < tabs.length; i++) {
    out.push(tabs[i]);
    var kids = tabs[i].getChildTabs() || [];
    for (var j = 0; j < kids.length; j++) {
      out.push(kids[j]);
      var gkids = kids[j].getChildTabs() || [];
      for (var m = 0; m < gkids.length; m++) out.push(gkids[m]);
    }
  }
  return out;
}

function _flattenSplits(splits) {
  var out = [];
  for (var i = 0; i < splits.length; i++) {
    out.push(splits[i]);
    var kids = splits[i].children || [];
    for (var j = 0; j < kids.length; j++) {
      out.push(kids[j]);
      var gkids = kids[j].children || [];
      for (var m = 0; m < gkids.length; m++) out.push(gkids[m]);
    }
  }
  return out;
}

function _describeTabs(flatAll) {
  // Compute depth by checking parent membership. Cheap because tab count is tiny.
  var depthOf = {};
  for (var t = 0; t < flatAll.length; t++) depthOf[flatAll[t].getId()] = 0;
  for (var i = 0; i < flatAll.length; i++) {
    var kids = flatAll[i].getChildTabs() || [];
    for (var j = 0; j < kids.length; j++) {
      depthOf[kids[j].getId()] = (depthOf[flatAll[i].getId()] || 0) + 1;
      var gkids = kids[j].getChildTabs() || [];
      for (var m = 0; m < gkids.length; m++) depthOf[gkids[m].getId()] = depthOf[kids[j].getId()] + 1;
    }
  }
  return flatAll.map(function (t) {
    return {id: t.getId(), title: t.getTitle(), depth: depthOf[t.getId()] || 0};
  });
}
