// app.js — the interface logic. No framework, no build step.
//
// ⚠️ THE ONLY THING THIS FILE MUST GET RIGHT IS HONESTY ABOUT WHAT A RESULT MEANS.
// The backend deliberately returns two kinds of hit and does not let them look alike:
//
//     lexical = true    the user's words ARE in this file   -> a finding
//     lexical = false   nearest in the embedding cone       -> a lead, not a finding
//
// ⚠️ That distinction exists because it was MEASURED, not assumed: a query for a string that is
// not on disk scored 0.649 against a real query's 0.632. **The boundary is negative**, so no
// similarity threshold can separate them. If the UI rendered both lists the same way, it would
// assert exactly the thing the backend refuses to assert.

const $ = (id) => document.getElementById(id);
const invoke = window.__TAURI__?.core?.invoke;

let TOKEN = null;
let PORT = 8734;
let results = [];       // flattened, in display order
let sel = 0;
let timer = null;

// ---------------------------------------------------------------- boot
async function boot() {
  if (!invoke) {
    setStatus("err", "Not running inside the app shell — open it with `cargo tauri dev`.");
    return;
  }
  try {
    TOKEN = await invoke("read_token");
    PORT = await invoke("daemon_port");
  } catch (e) {
    // ⚠️ A MISSING TOKEN IS THE MOST LIKELY FIRST-RUN FAILURE, and the reason is almost always
    // "the daemon has never been started". Say that, rather than showing a stack trace.
    setStatus("err", `Cannot read the API token (${e}). Start the daemon once:  ds index`);
    return;
  }
  const h = await api("/api/health", { auth: false });
  if (h.ok) {
    $("indexinfo").textContent =
      `${h.documents.toLocaleString()} documents indexed` +
      (h.semantic ? ` · ${h.semantic}` : " · semantic off");
    setStatus("", "");
    // ⚠️ THE BANNER IS DRIVEN BY THE DAEMON, NOT GUESSED BY THE UI. Only the engine knows which
    // documents are embedded and whether the vectors still describe them. A frontend computing
    // this itself would draw a confident progress bar attached to nothing.
    applyIndexState(h);
    // ⚠️⚠️ THE FIRST-RUN DECISION. An empty index with no message is what made the app look
    // broken: it launched, showed a search box that returned nothing, and gave no hint that a
    // terminal command was expected. If there is nothing to search, say so and offer the fix.
    if (h.documents === 0 || !h.documents) {
      Setup.open();
    } else if (h.indexing) {
      startPolling();
    }
  } else {
    setStatus("err", "The search daemon is not responding on 127.0.0.1:" + PORT);
  }
  $("q").focus();
}

// ⚠️ ONE STATE OBJECT, TWO RENDERINGS. An in-progress index and a stale vector set are
// DIFFERENT problems — one resolves by waiting, the other does not — so they must not share a
// message. Showing "indexing…" for stale vectors would tell the user to wait for something that
// is not happening.
// ⚠️ A PLAIN SETTER, separate from applyIndexState, because these are messages the APP wants to
// show ("could not reach the daemon") rather than states the ENGINE reports.
function setBanner(cls, text) {
  const bar = $("banner");
  if (!cls) { bar.className = ""; bar.textContent = ""; return; }
  bar.className = "show " + cls;
  bar.textContent = text;
}

function applyIndexState(h) {
  const bar = $("banner");
  const pct = h.embedding_percent || 0;
  if (h.indexing) {
    bar.className = "show info";
    bar.textContent = `Indexing… ${pct.toFixed(0)}% — results are incomplete`;
  } else if (h.live && h.live.running) {
    // ⚠️ A RUN STARTED SOMEWHERE ELSE — a terminal, or a previous launch. The daemon cannot
    // compute this itself; it comes from the status file the indexing process writes, including
    // the ETA. Without it the app would show a stale banner for a job it cannot see.
    const p = h.live.percent;
    bar.className = "show info";
    bar.textContent = `Indexing${p != null ? " " + p.toFixed(0) + "%" : "…"} — results are ` +
                      `incomplete` +
                      (h.live.eta_seconds ? `  ·  about ${fmtEta(h.live.eta_seconds)} left` : "");
  } else if (h.stale && h.stale.stale) {
    if (h.stale.severity === "incomplete") {
      bar.className = "show warn";
      bar.textContent = `⚠️ Embedding stopped at ${pct.toFixed(0)}% — meaning-based results ` +
                        `cover only part of the index. Exact and keyword matches are complete.`;
    } else {
      bar.className = "show warn";
      bar.textContent = `⚠️ Semantic index is out of date — ${h.stale.reason}. ` +
                        `Re-run \`ds index\`. Exact and keyword matches are unaffected.`;
    }
  } else {
    bar.className = "";
    bar.textContent = "";
  }
}

// ⚠️ POLLED WHILE INDEXING, and the poll stops when it stops. A permanent 2-second timer in a
// tray app that is mostly idle is a battery cost for no information.
let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    const h = await api("/api/health", { auth: false });
    if (!h.ok) return;
    applyIndexState(h);
    if (!h.indexing) { clearInterval(pollTimer); pollTimer = null; }
  }, 2000);
}

function setStatus(cls, text) {
  const el = $("status");
  el.className = cls;
  el.textContent = text;
}

// ---------------------------------------------------------------- api
async function api(path, opts = {}) {
  const headers = {};
  if (opts.auth !== false) headers["X-DS-Token"] = TOKEN;
  const init = { headers, method: opts.method || "GET" };
  if (opts.body !== undefined) {
    // ⚠️ The Content-Type matters: without it the server's json.loads gets an empty body, and
    // the request looks like it arrived with no data rather than being malformed.
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}${path}`, init);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return { ok: false, error: body.error || `HTTP ${r.status}` };
    return { ok: true, ...body };
  } catch (e) {
    // ⚠️ A NETWORK ERROR AND AN EMPTY RESULT SET MUST NOT LOOK THE SAME. A search that failed
    // and a search that found nothing are different facts; showing an empty list for both is
    // how a user concludes their file does not exist.
    return { ok: false, error: `daemon unreachable (${e.message})` };
  }
}

// ---------------------------------------------------------------- search
function onType() {
  clearTimeout(timer);
  const q = $("q").value.trim();
  if (!q) { results = []; render(); setStatus("", ""); return; }
  // ⚠️ DEBOUNCED, but short. 120ms is below the threshold where typing feels laggy, and the
  // daemon answers in ~60ms so the result arrives while the user is still typing.
  timer = setTimeout(() => run(q), 120);
}

async function run(q) {
  const r = await api(`/api/search?q=${encodeURIComponent(q)}&k=25`);
  if (!r.ok) { setStatus("err", r.error); results = []; render(); return; }
  // ⚠️ Ignore a response that arrived for a query the user has already moved past. Without
  // this, a slow response overwrites a fast one and the list shows results for an older query.
  if ($("q").value.trim() !== q) return;

  results = r.results.map((x) => ({ ...x, group: x.lexical ? "match" : "meaning" }));
  sel = results.findIndex((x) => x.lexical);
  if (sel < 0) sel = 0;

  const parts = [`${r.took_ms}ms`, `routed as ${r.kind}`];
  if (r.broadened) parts.push("broadened after an empty first pass");
  if (r.llm && r.llm.blocked) parts.push(`⚠️ ${r.llm.blocked} snippet(s) blocked by the secret interlock`);
  setStatus("", parts.join(" · "));
  render();
  $("q").focus();
}

// ---------------------------------------------------------------- render
function render() {
  const box = $("results");
  box.textContent = "";
  if (!results.length) return;

  let lastGroup = null;
  for (let i = 0; i < results.length; i++) {
    const r = results[i];
    if (r.group !== lastGroup) {
      lastGroup = r.group;
      const h = document.createElement("div");
      h.className = "group" + (r.group === "meaning" ? " meaning" : "");
      h.textContent = r.group === "match" ? "Matches — your words are in these files"
                                          : "Closest by meaning — no word match";
      if (r.group === "meaning") {
        // ⚠️ THE DISCLAIMER IS PART OF THE UI, NOT A COMMENT IN THE CODE. A user who does not
        // read the source has no other way to learn that these are leads rather than findings.
        const n = document.createElement("span");
        n.className = "note";
        n.textContent = "⚠️ Not evidence the thing exists — nearest in the index, nothing more.";
        h.appendChild(n);
      }
      box.appendChild(h);
    }
    const row = document.createElement("div");
    row.className = "row" + (i === sel ? " sel" : "");

    const type = document.createElement("div");
    type.className = "type";
    type.textContent = (r.lang || "").slice(0, 4);

    const mid = document.createElement("div");
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = r.path;
    if (r.line) {
      const ln = document.createElement("span");
      ln.className = "ln";
      ln.textContent = ":" + r.line;
      name.appendChild(ln);
    }
    const snip = document.createElement("div");
    snip.className = "snip";
    snip.textContent = (r.snippet || "").slice(0, 140);
    mid.append(name, snip);

    const via = document.createElement("div");
    via.className = "via";
    // ⚠️ OCR IS MARKED. A hit found inside a screenshot by OCR is a weaker claim than one read
    // from a text file, and the user checking it needs to know which they are relying on.
    via.textContent = r.via.join("+");
    if (r.lang === "ocr") {
      const t = document.createElement("span");
      t.className = "tag-ocr";
      t.textContent = " · ocr";
      via.appendChild(t);
    }

    row.append(type, mid, via);
    row.onclick = () => { sel = i; render(); open(false); };
    box.appendChild(row);
  }
}

// ---------------------------------------------------------------- actions
function move(d) {
  if (!results.length) return;
  sel = Math.max(0, Math.min(results.length - 1, sel + d));
  render();
  document.querySelector(".row.sel")?.scrollIntoView({ block: "nearest" });
}

async function open(reveal) {
  const r = results[sel];
  if (!r) return;
  // ⚠️ OPENING IS DELEGATED TO THE SHELL. A webview cannot launch a file manager; the Rust side
  // can. ⚠️ And the path is taken from `full_path`, never reconstructed from the display path —
  // the display path has `~` substituted and is not a real filesystem location.
  if (invoke) {
    try { await invoke("open_path", { path: r.full_path, reveal }); }
    catch (e) { setStatus("err", `cannot open: ${e}`); }
  }
  hideWindow();
}

function hideWindow() {
  if (invoke) invoke("hide_window").catch(() => {});
}

function fmtEta(sec) {
  // ⚠️ Rounded to something a person would say. "4h 12m" is actionable; "15120.4s" is not.
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

// ---------------------------------------------------------------- events
$("q").addEventListener("input", onType);
window.addEventListener("keydown", (e) => {
  // ⚠️ SETUP MUST BE REACHABLE AFTER FIRST RUN. A one-shot wizard that cannot be reopened makes
  // "add another folder" impossible without deleting the index.
  if (e.key === "," && (e.metaKey || e.ctrlKey)) { e.preventDefault(); Setup.open(); return; }
  if (e.key === "Escape") {
    e.preventDefault();
    if (Setup.isOpen()) { Setup.close(); return; }
    hideWindow();
  }
  else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
  else if (e.key === "Enter") { e.preventDefault(); open(e.metaKey || e.ctrlKey); }
});
// ⚠️ The OS focus and the DOM focus are separate. Showing the window does not put the cursor in
// the box, so the shell emits an event and the box claims focus here.
if (window.__TAURI__?.event) {
  window.__TAURI__.event.listen("focus-input", () => $("q").select());
}
// ⚠️ THE SETTINGS AFFORDANCE, because a first-run wizard that cannot be reopened makes "add
// another folder" impossible without deleting the index.
$("settings").addEventListener("click", () => Setup.open());

// ⚠️ Clear when hidden. A search box that reopens showing the previous query makes the user
// select-and-delete every time; Spotlight starts empty.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { $("q").value = ""; results = []; render(); setStatus("", ""); }
});

boot();
