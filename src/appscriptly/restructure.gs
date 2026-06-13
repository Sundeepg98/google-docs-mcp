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
 */

function doPost(e) {
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
