/**
 * Behavioral test for the Apps Script /exec HMAC verify path.
 *
 * WHY THIS EXISTS: Google Apps Script web apps NEVER populate e.headers on
 * the doPost(e) event. The runtime delivers e.parameter, e.parameters,
 * e.postData, e.queryString and e.contentLength, but NOT the HTTP request
 * headers. A verify implementation that reads the signature from e.headers
 * therefore rejects EVERY request once a key is provisioned (fail-closed
 * becomes fail-always), which bricks the restructure /exec feature. The only
 * transport the runtime actually delivers is the query string, surfaced as
 * e.parameter, so the signature must travel as ?mcp_ts=...&mcp_sig=... on
 * the /exec URL.
 *
 * This test evaluates the REAL .gs source under Node (vm sandbox) with an
 * Apps-Script-shaped Utilities shim, and drives the verify function with a
 * REALISTIC event object (e.headers absent, exactly like production). It
 * asserts the verify:
 *   1. ACCEPTS a correctly signed request (mcp_ts + mcp_sig query params);
 *   2. REJECTS forged / tampered / stale / future / missing / replayed /
 *      malformed requests;
 *   3. REJECTS everything when unconfigured (sentinels intact): fail closed;
 *   4. does NOT authenticate via a channel the runtime cannot populate
 *      (a signature offered only in e.headers must not verify).
 *
 * Usage:
 *   node tests/js/verify_hmac_behavior.test.mjs
 *     Runs against src/appscriptly/restructure.gs (_verifyHmac), applying
 *     the same sentinel substitution the deploy path performs
 *     (apps_script_hmac.inject_hmac_into_source).
 *
 *   node tests/js/verify_hmac_behavior.test.mjs <source.gs> <verifyFn> <key>
 *     Runs the same behavioral suite against an externally prepared source,
 *     e.g. the gas_deploy auto-injected guard (__mcpVerifyWebappHmac)
 *     produced by inject_webapp_hmac_guard. The pytest wrapper
 *     (tests/unit/test_restructure_gs_verify_behavior.py) uses this mode.
 *
 * Exit code 0 = all cases pass; 1 = at least one failure (report on stdout).
 */

import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const RESTRUCTURE_GS = path.join(
  REPO_ROOT, "src", "appscriptly", "restructure.gs",
);

// Same shape generate_hmac_key() mints: 64 lowercase hex chars (256 bits).
const TEST_KEY = "ab".repeat(32);
const WRONG_KEY = "cd".repeat(32);

/**
 * Apps Script Utilities shim. computeHmacSha256Signature(message, key) with
 * string args treats both as UTF-8 and returns a JS array of SIGNED bytes
 * (Java byte semantics, -128..127). The .gs code masks each byte back to
 * 0..255, so the shim must reproduce the signedness to be a faithful model.
 */
const UtilitiesShim = {
  computeHmacSha256Signature(message, key) {
    const mac = crypto
      .createHmac("sha256", Buffer.from(key, "utf8"))
      .update(Buffer.from(message, "utf8"))
      .digest();
    return Array.from(mac, (b) => (b > 127 ? b - 256 : b));
  },
};

/** Server-side signer, mirroring apps_script_hmac.compute_signature. */
function sign(key, timestamp, body) {
  return crypto
    .createHmac("sha256", Buffer.from(key, "utf8"))
    .update(Buffer.from(`${timestamp}.${body}`, "utf8"))
    .digest("hex");
}

/**
 * Evaluate a .gs source in an isolated context and return the named verify
 * function. Top-level .gs code only declares vars + functions; Google
 * service globals (DocumentApp, ContentService) are referenced inside
 * function bodies we never call, so they need no shims here.
 */
function loadVerifier(sourceText, fnName) {
  const context = vm.createContext({ Utilities: UtilitiesShim });
  vm.runInContext(sourceText, context, { filename: "under-test.gs" });
  const fn = context[fnName];
  if (typeof fn !== "function") {
    throw new Error(`source did not define a function named ${fnName}`);
  }
  return fn;
}

/**
 * Build a REALISTIC Apps Script doPost(e) event. Model exactly what the
 * runtime delivers: parameter / parameters / queryString / contentLength /
 * postData. Crucially there is NO headers property, matching production.
 * Pass extra: {headers: {...}} only to model a hypothetical runtime that
 * DID expose headers (used to pin that headers are not a trusted channel).
 */
function makeEvent(body, params, extra = {}) {
  const parameter = {};
  const parameters = {};
  for (const [k, v] of Object.entries(params)) {
    parameter[k] = String(v);
    parameters[k] = [String(v)];
  }
  const event = {
    parameter,
    parameters,
    queryString: Object.entries(params)
      .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
      .join("&"),
    contentLength: Buffer.byteLength(body, "utf8"),
    postData: {
      contents: body,
      length: Buffer.byteLength(body, "utf8"),
      type: "application/json",
      name: "postData",
    },
    ...extra,
  };
  return event;
}

const BODY = JSON.stringify({ docId: "DOC-1", splitTree: [] });

/** The behavioral suite. Returns an array of {name, pass, detail}. */
function runSuite(verify, key) {
  const now = Math.floor(Date.now() / 1000);
  const ts = String(now);
  const results = [];

  function check(name, event, expectOk) {
    let outcome;
    try {
      outcome = verify(event);
    } catch (err) {
      results.push({
        name,
        pass: false,
        detail: `verify threw: ${err && err.message}`,
      });
      return;
    }
    const ok = !!(outcome && outcome.ok);
    const pass = ok === expectOk;
    results.push({
      name,
      pass,
      detail: pass
        ? ""
        : `expected ok=${expectOk}, got ${JSON.stringify(outcome)}`,
    });
  }

  // 1. The happy path a correctly configured server produces: fresh unix
  //    timestamp + valid signature, carried as query params, no headers.
  const validEvent = makeEvent(BODY, {
    mcp_ts: ts,
    mcp_sig: sign(key, ts, BODY),
  });
  if ("headers" in validEvent) {
    throw new Error("test bug: realistic event must not carry headers");
  }
  check("accepts correctly signed request via query params", validEvent, true);

  // 2. Uppercase hex from a client is tolerated (verify lowercases).
  check(
    "accepts uppercase hex signature",
    makeEvent(BODY, { mcp_ts: ts, mcp_sig: sign(key, ts, BODY).toUpperCase() }),
    true,
  );

  // 3. No signature params at all. This is EXACTLY what the runtime
  //    delivers when a client sends the signature only as HTTP headers,
  //    because Apps Script strips headers from the event.
  check("rejects request with no signature params", makeEvent(BODY, {}), false);

  // 4. Signature offered ONLY via a hypothetical headers channel must not
  //    authenticate: the runtime cannot populate e.headers, so treating it
  //    as a trusted transport is both dead code and a modeling error.
  check(
    "rejects signature offered only in e.headers",
    makeEvent(BODY, {}, {
      headers: { "X-MCP-Signature": sign(key, ts, BODY), "X-MCP-Timestamp": ts },
    }),
    false,
  );

  // 5. Forged signature (attacker without the key).
  check(
    "rejects forged signature (wrong key)",
    makeEvent(BODY, { mcp_ts: ts, mcp_sig: sign(WRONG_KEY, ts, BODY) }),
    false,
  );

  // 6. Valid signature over a DIFFERENT body (tamper in flight).
  check(
    "rejects tampered body",
    makeEvent(JSON.stringify({ docId: "VICTIM", splitTree: [] }), {
      mcp_ts: ts,
      mcp_sig: sign(key, ts, BODY),
    }),
    false,
  );

  // 7. Stale timestamp beyond the 300s skew window (captured replay).
  const staleTs = String(now - 4000);
  check(
    "rejects stale timestamp outside skew window",
    makeEvent(BODY, { mcp_ts: staleTs, mcp_sig: sign(key, staleTs, BODY) }),
    false,
  );

  // 8. Future timestamp beyond the window.
  const futureTs = String(now + 4000);
  check(
    "rejects future timestamp outside skew window",
    makeEvent(BODY, { mcp_ts: futureTs, mcp_sig: sign(key, futureTs, BODY) }),
    false,
  );

  // 9. Replay of a captured (old ts, old sig) pair under a FRESH ts param:
  //    the signature binds the timestamp, so it must not verify.
  check(
    "rejects captured signature replayed with fresh timestamp",
    makeEvent(BODY, { mcp_ts: ts, mcp_sig: sign(key, staleTs, BODY) }),
    false,
  );

  // 10. Malformed timestamp.
  check(
    "rejects malformed timestamp",
    makeEvent(BODY, { mcp_ts: "not-a-number", mcp_sig: sign(key, "not-a-number", BODY) }),
    false,
  );

  // 11. Missing timestamp with a present signature.
  check(
    "rejects missing timestamp",
    makeEvent(BODY, { mcp_sig: sign(key, ts, BODY) }),
    false,
  );

  return results;
}

function report(title, results) {
  let failures = 0;
  console.log(`\n== ${title} ==`);
  for (const r of results) {
    if (r.pass) {
      console.log(`  PASS  ${r.name}`);
    } else {
      failures += 1;
      console.log(`  FAIL  ${r.name}: ${r.detail}`);
    }
  }
  return failures;
}

function main() {
  const [, , sourceArg, fnArg, keyArg] = process.argv;
  let totalFailures = 0;

  if (sourceArg) {
    // External mode: pre-templated source + verify fn + key from the caller
    // (used for the gas_deploy auto-injected guard).
    const source = fs.readFileSync(sourceArg, "utf8");
    const verify = loadVerifier(source, fnArg || "_verifyHmac");
    totalFailures += report(
      `${path.basename(sourceArg)} :: ${fnArg}`,
      runSuite(verify, keyArg || TEST_KEY),
    );
  } else {
    // Default mode: the shipped restructure.gs with the same substitution
    // inject_hmac_into_source performs at deploy time.
    const raw = fs.readFileSync(RESTRUCTURE_GS, "utf8");
    const configured = raw
      .replaceAll("__MCP_HMAC_KEY__", TEST_KEY)
      .replaceAll("__MCP_HMAC_REQUIRED__", "true");
    const verify = loadVerifier(configured, "_verifyHmac");
    totalFailures += report(
      "restructure.gs :: _verifyHmac (key provisioned)",
      runSuite(verify, TEST_KEY),
    );

    // Fail-closed when NOT configured: sentinels intact, so even a
    // correctly signed request must be rejected.
    const unconfigured = loadVerifier(raw, "_verifyHmac");
    const now = String(Math.floor(Date.now() / 1000));
    const outcome = unconfigured(
      makeEvent(BODY, { mcp_ts: now, mcp_sig: sign(TEST_KEY, now, BODY) }),
    );
    const failClosed = !(outcome && outcome.ok);
    totalFailures += report("restructure.gs :: _verifyHmac (unconfigured)", [
      {
        name: "rejects everything when key not templated in (fail closed)",
        pass: failClosed,
        detail: failClosed ? "" : `expected rejection, got ${JSON.stringify(outcome)}`,
      },
    ]);
  }

  if (totalFailures > 0) {
    console.log(`\n${totalFailures} case(s) FAILED`);
    process.exit(1);
  }
  console.log("\nall cases passed");
}

main();
