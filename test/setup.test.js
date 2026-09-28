/**
 * test/setup.test.js — the setup modal, tested by DRIVING IT, not by reading it.
 *
 * ============================================================================
 * ⚠️⚠️ WHY THIS FILE EXISTS
 * ============================================================================
 * Three rounds of bugs in this one panel were missed by reading the source, and the
 * "verification" that replaced reading was worse: regex assertions over the file text that
 *
 *     - PASSED while two of three bugs were present, and
 *     - FAILED while the code was correct,
 *
 * in the same day. A regex over source text proves a string exists somewhere. It cannot
 * prove that clicking a button produces a row.
 *
 * ⚠️ SO EVERY TEST HERE ASSERTS ON THE RENDERED DOM AFTER AN ACTION. Never on the source,
 * never on an internal variable. If the test passes and the feature is broken, the test is
 * wrong — and the way to notice is that the assertion names something a USER would see.
 *
 * ============================================================================
 * ⚠️ THE HARNESS, AND THE PART THAT IS EASY TO GET WRONG
 * ============================================================================
 * setup.js is not a module. It declares `const Setup = (...)()` at top level and reads
 * `api`, `invoke`, `setBanner` and `startPolling` from the global scope.
 *
 * ⚠️ A top-level `const` in eval does NOT become a property of the window — so loading it
 * and then reading `window.Setup` returns undefined and every test fails for a reason that
 * has nothing to do with the code. The file is therefore evaluated with an explicit
 * `window.Setup = Setup;` appended.
 */

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.resolve(__dirname, "..");
const HTML = fs.readFileSync(path.join(ROOT, "ui/index.html"), "utf8");
const SETUP_JS = fs.readFileSync(path.join(ROOT, "ui/setup.js"), "utf8");

// ---------------------------------------------------------------------------
// results
// ---------------------------------------------------------------------------
let passed = 0;
let failed = 0;
const failures = [];

function check(label, cond, detail) {
  if (cond) {
    passed++;
    console.log(`  PASS  ${label}`);
  } else {
    failed++;
    failures.push(label);
    console.log(`  FAIL  ${label}${detail ? "\n          " + detail : ""}`);
  }
}

// ⚠️ EVERY WAIT IS A REAL TICK, NOT A GUESS. refresh() is async and render() happens in its
// continuation; asserting before it resolves tests the previous frame.
const tick = () => new Promise((r) => setTimeout(r, 0));

// ---------------------------------------------------------------------------
// the fixture
// ---------------------------------------------------------------------------
const SUGGESTED = [
  { path: "/home/u/Documents", name: "Documents", exists: true },
  { path: "/home/u/Desktop", name: "Desktop", exists: true },
];

function makeEnv(opts = {}) {
  const dom = new JSDOM(HTML, { url: "http://localhost/", runScripts: "outside-only" });
  const { window } = dom;

  const calls = [];          // every api() call, in order
  const notes = [];          // setBanner / note output

  window.api = async (p, o = {}) => {
    calls.push({ path: p, method: o.method || "GET", body: o.body });
    if (p === "/api/roots" && !o.method) {
      if (opts.rootsFails) return { ok: false, error: "engine unreachable" };
      return { ok: true, suggested: SUGGESTED,
               indexed: opts.indexed || [], configured: opts.configured || [] };
    }
    if (p === "/api/index") {
      if (opts.indexFails) return { ok: false, error: "already running" };
      return { ok: true, accepted: true };
    }
    return { ok: true, roots: [] };
  };

  window.invoke = async (cmd, args) => {
    calls.push({ invoke: cmd, args });
    if (cmd === "pick_folder") {
      if (opts.pickFails) throw new Error("no dialog");
      return opts.pickReturns || ["/home/u/Projects"];
    }
    return null;
  };
  window.setBanner = (cls, msg) => notes.push({ cls, msg });
  window.startPolling = () => {};
  window.Polling = { start: () => {} };

  // ⚠️ see the header: the explicit export is what makes this loadable at all
  window.eval(SETUP_JS + "\n;window.Setup = Setup;");

  return { dom, window, doc: window.document, calls, notes,
           $: (id) => window.document.getElementById(id) };
}

// ⚠️ THE QUERY HELPERS READ THE RENDERED DOM. `rows` counts what the user can see, and it is
// the number the original bug made wrong.
const rows = (env) => [...env.doc.querySelectorAll("#setup .setup-row:not(.whole)")];
const rowPaths = (env) => rows(env).map((r) => r.querySelector(".setup-path").textContent);
const rowNames = (env) => rows(env).map((r) => r.querySelector(".setup-name").textContent);
const boxes = (env) => rows(env).map((r) => r.querySelector("input[type=checkbox]"));
const goButton = (env) => [...env.doc.querySelectorAll("#setup .setup-foot button")]
  .find((b) => b.classList.contains("primary"));
const msg = (env) => env.$("setup-msg").textContent.trim();
const overlayShown = (env) => env.$("setup-overlay").classList.contains("show");

// ===========================================================================
async function main() {
  console.log("\n  setup modal — driving the real DOM\n");

  // ------------------------------------------------------------------ 1
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    check("open() shows the overlay", overlayShown(env));
    check("one row per suggested folder", rows(env).length === 2, `got ${rows(env).length}`);
    check("existing folders are pre-ticked", boxes(env).every((b) => b.checked));
  }

  // ------------------------------------------------------------------ 2  ⚠️ THE ORIGINAL BUG
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    const before = rows(env).length;
    env.doc.querySelector("#setup .setup-add button.pick").click();
    await tick(); await tick();
    const after = rows(env).length;
    check("⚠️ a PICKED folder appears as a row in the DOM",
          after === before + 1, `rows ${before} -> ${after}`);
    check("⚠️ and it is ticked",
          boxes(env).some((b) => b.checked) && rows(env).length === 3);
    check("the picked path is what the picker returned",
          rowPaths(env).some((p) => p.includes("/home/u/Projects")),
          rowPaths(env).join(" | "));
  }

  // ------------------------------------------------------------------ 3
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    env.doc.querySelector("#setup .setup-add button.pick").click();
    await tick(); await tick();
    const x = [...env.doc.querySelectorAll("#setup .setup-x")];
    check("a picked folder has a remove button", x.length === 1, `found ${x.length}`);
    const before = rows(env).length;
    x[0].click(); await tick();
    check("⚠️ clicking it REMOVES the row", rows(env).length === before - 1,
          `rows ${before} -> ${rows(env).length}`);
    check("suggested folders are NOT removable",
          !rows(env).some((r) => r.querySelector(".setup-x") &&
                                 !rowPaths(env).includes("/home/u/Projects")));
  }

  // ------------------------------------------------------------------ 4
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    const bs = boxes(env);
    bs.forEach((b) => { b.checked = false; b.dispatchEvent(new env.window.Event("change")); });
    await tick(); await tick();
    check("⚠️ unticking everything DISABLES the Index button",
          goButton(env).disabled === true);
    const b0 = boxes(env)[0];
    b0.checked = true; b0.dispatchEvent(new env.window.Event("change"));
    await tick(); await tick();
    check("re-ticking one ENABLES it", goButton(env).disabled === false);
  }

  // ------------------------------------------------------------------ 5
  {
    const env = makeEnv({ indexed: [{ path: "/home/u/Documents", documents: 5570 }] });
    await env.window.Setup.open(); await tick();
    check("the selection summary is shown",
          /Selected/.test(env.$("setup").textContent),
          env.$("setup").textContent.slice(0, 120));
    const est = env.doc.querySelector("#setup .setup-est");
    check("⚠️ and it names how many documents are indexed",
          est && /5,570/.test(est.textContent), est ? est.textContent : "(no .setup-est)");
  }

  // ------------------------------------------------------------------ 6
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    const wcb = env.doc.querySelector("#setup .setup-row.whole input");
    wcb.checked = true; wcb.dispatchEvent(new env.window.Event("change"));
    await tick(); await tick();
    check("⚠️ entire-device WARNS that the folders are ignored",
          /ignored/i.test(msg(env)), `message was: "${msg(env)}"`);
    wcb.checked = false; wcb.dispatchEvent(new env.window.Event("change"));
    await tick(); await tick();
    check("and the warning clears when it is unticked", msg(env) === "");
  }

  // ------------------------------------------------------------------ 7  ⚠️ THE HIDDEN-ERROR BUG
  {
    const env = makeEnv({ rootsFails: true });
    await env.window.Setup.open(); await tick();
    check("⚠️ a failed /api/roots does NOT leave a blank panel", rows(env).length === 0);
    check("⚠️ it SAYS what failed, in the panel",
          /Could not load/.test(msg(env)), `message was: "${msg(env)}"`);
    check("nothing was written to the (covered) banner",
          env.notes.length === 0, JSON.stringify(env.notes));
  }

  // ------------------------------------------------------------------ 8
  {
    const env = makeEnv({ indexFails: true });
    await env.window.Setup.open(); await tick();
    goButton(env).click();
    await tick(); await tick(); await tick();
    check("⚠️ a failed index KEEPS THE PANEL OPEN", overlayShown(env));
    check("⚠️ and shows the reason in the panel",
          /Could not start indexing/.test(msg(env)), `message was: "${msg(env)}"`);
  }

  // ------------------------------------------------------------------ 9
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    goButton(env).click();
    await tick(); await tick(); await tick();
    check("a successful start CLOSES the panel", !overlayShown(env));
    const rootsCall = env.calls.find((c) => c.path === "/api/roots" && c.method === "POST");
    check("it POSTs the chosen roots", rootsCall && rootsCall.body.roots.length === 2,
          JSON.stringify(rootsCall && rootsCall.body));
    check("and an explicit mode", rootsCall && rootsCall.body.mode === "explicit");
    check("then starts indexing",
          env.calls.some((c) => c.path === "/api/index" && c.method === "POST"));
  }

  // ------------------------------------------------------------------ 10
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    const wcb = env.doc.querySelector("#setup .setup-row.whole input");
    wcb.checked = true; wcb.dispatchEvent(new env.window.Event("change"));
    await tick(); await tick();
    goButton(env).click();
    await tick(); await tick(); await tick();
    const rc = env.calls.find((c) => c.path === "/api/roots" && c.method === "POST");
    check("⚠️ entire-device sends mode:'everything'",
          rc && rc.body.mode === "everything", JSON.stringify(rc && rc.body));
  }

  // ------------------------------------------------------------------ 11
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    check("isOpen() is true while open", env.window.Setup.isOpen() === true);
    const q = env.$("q");
    q.blur();
    env.window.Setup.close(); await tick();
    check("close() hides the overlay", !overlayShown(env));
    check("⚠️ close() returns focus to the search box",
          env.doc.activeElement === q,
          `activeElement is ${env.doc.activeElement && env.doc.activeElement.id}`);
    check("isOpen() is false afterwards", env.window.Setup.isOpen() === false);
  }

  // ------------------------------------------------------------------ 12
  {
    const env = makeEnv({ pickFails: true });
    await env.window.Setup.open(); await tick();
    env.doc.querySelector("#setup .setup-add button.pick").click();
    await tick(); await tick();
    check("⚠️ a picker failure is REPORTED, not silent",
          /picker could not open/i.test(msg(env)), `message was: "${msg(env)}"`);
    const inp = env.doc.querySelector("#setup .setup-add input");
    check("and the text-path fallback becomes visible",
          inp && inp.style.display !== "none");
  }

  // ------------------------------------------------------------------ 13
  {
    const env = makeEnv();
    await env.window.Setup.open(); await tick();
    const inp = env.doc.querySelector("#setup .setup-add input");
    inp.value = "/home/u/Typed";
    inp.dispatchEvent(new env.window.KeyboardEvent("keydown", { key: "Enter" }));
    await tick(); await tick();
    check("⚠️ a TYPED path becomes a row too",
          rowPaths(env).some((p) => p.includes("/home/u/Typed")),
          rowPaths(env).join(" | "));
  }

  // ---------------------------------------------------------------------------
  console.log(`\n  ${passed} passed, ${failed} failed`);
  if (failed) {
    console.log("\n  failures:");
    failures.forEach((f) => console.log(`    - ${f}`));
  }
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error("\n  harness crashed:", e);
  process.exit(2);
});
