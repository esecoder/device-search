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
let results = [];
let answer = null;      // ⚠️ a sentence the user will believe, unlike a list they can check       // flattened, in display order
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
    // ⚠️ NO TERMINAL COMMAND. If the app cannot start its own engine, that is the APP's failure
    // and the message should say what happened, not hand the user homework.
    setStatus("err", `The search engine could not be started (${e}).`);
    return;
  }
  // ⚠️⚠️ WAIT FOR THE ENGINE, DO NOT CHECK IT ONCE.
  //
  // The shell spawns the Python daemon and then immediately asks whether it is healthy. Starting
  // a Python process takes a moment to bind the port, so the FIRST check ALWAYS FAILS — and the
  // old code had no retry, so it displayed "the search daemon is not responding" permanently and
  // never looked again. The daemon was fine a second later and the user was told otherwise for
  // the rest of the session.
  //
  // ⚠️ AND THE MESSAGE WAS WRONG TOO. "Not responding" describes a crash. What was actually
  // happening is normal startup, and saying so is the difference between waiting and quitting.
  let h = await api("/api/health", { auth: false });
  if (!h.ok) {
    // ⚠️ MEASURE THE ELAPSED TIME, DO NOT USE THE LOOP COUNTER.
    // The old code printed `(${i}s)` where `i` was an ITERATION INDEX — and each iteration is a
    // 500ms sleep PLUS a failed fetch that has to time out, so it counted to 39 while claiming
    // 20 and the count was never seconds at all. ⚠️ A number labelled with the wrong unit is
    // worse than no number: it makes the user distrust every other figure on screen.
    const t0 = Date.now();
    while (!h.ok && Date.now() - t0 < 30000) {
      // ⚠️ SHOW THE REAL REASON WHILE WAITING. The retry loop used to print only a counter and
      // discard h.error — which is the string that says WHY, and it was computed every time and
      // thrown away. Diagnosing this took a macOS ATS investigation that one visible error
      // message would have short-circuited.
      setStatus("", `starting the search engine… ${((Date.now() - t0) / 1000).toFixed(0)}s` +
                    (h.error ? `   [${h.error}]` : ""));
      await new Promise((r) => setTimeout(r, 500));
      h = await api("/api/health", { auth: false });
    }
  }
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
    // ⚠️ ONLY REACHED AFTER ~20 SECONDS OF RETRIES, so this now means what it says. It also gives
    // the command that produces the real error, because "not responding" alone is unactionable.
    // ⚠️ POINT AT THE LOG, NOT AT A COMMAND. The shell now writes the daemon's own output to
    // ~/.device-search/daemon.log, so the reason is in a file rather than lost to /dev/null.
    setStatus("err", `The search engine did not start. ` +
                     `${h.error ? h.error + ". " : ""}` +
                     `Details are in ~/.device-search/daemon.log`);
  }
  $("q").focus();

  // ⚠️ THE TOGGLE ONLY EXISTS WHEN IT CAN DO SOMETHING. A switch whose only possible
  // outcome is an error is worse than no switch: it teaches the user that the feature is
  // broken rather than that it is not set up.
  const refreshAsk = async () => {
    const b = $("askbtn");
    if (!b) return;
    try {
      const lr = await api("/api/llm");
      b.style.display = lr.ok && lr.configured ? "" : "none";
    } catch (e) { b.style.display = "none"; }
  };
  refreshAsk();
  if ($("askbtn")) {
    $("askbtn").onclick = () => {
      $("askbtn").classList.toggle("on");
      const q = $("q").value.trim();
      if (q) run(q);      // ⚠️ re-run, so the toggle does something visible immediately
    };
  }
}

// ⚠️ ONE STATE OBJECT, TWO RENDERINGS. An in-progress index and a stale vector set are
// DIFFERENT problems — one resolves by waiting, the other does not — so they must not share a
// message. Showing "indexing…" for stale vectors would tell the user to wait for something that
// is not happening.
// ⚠️ A PLAIN SETTER, separate from applyIndexState, because these are messages the APP wants to
// show ("could not reach the daemon") rather than states the ENGINE reports.
function setBanner(cls, msg) {
  // ⚠️ WRITES TO #banner-text, NOT #banner. Setting textContent on the container DELETES the
  // progress bar element inside it, so the bar would disappear the first time any message was
  // set — a bug that only appears on the second banner.
  const bar = $("banner");
  const t = $("banner-text");
  if (!cls) { bar.className = ""; if (t) t.textContent = ""; return; }
  bar.className = "show " + cls;
  if (t) t.textContent = msg;
}

function applyIndexState(h) {
  // ⚠️⚠️ WHAT THIS SAYS, AND WHAT IT MUST NEVER SAY.
  //
  // The message it replaces read:
  //
  //   "Semantic index is out of date — document count changed (5,573 -> 5,570); files were
  //    modified. Re-run `ds index`. Exact and keyword matches are unaffected."
  //
  // ⚠️ FIVE THINGS WRONG WITH THAT, in order of how much they matter:
  //   1. It told the user to run a TERMINAL COMMAND. A shipped app must never do that.
  //   2. "semantic index", "document count changed", "exact and keyword matches" — someone
  //      searching their own files does not know what those are and should not have to.
  //   3. It reported a NUMBER DIFFERENCE (5,573 -> 5,570) as if three were a quantity worth
  //      reading. It is not. The user needs to know results might be wrong, not by how much.
  //   4. It implied the user had to act, when the app repairs itself automatically.
  //   5. It was three wrapped lines that pushed the results down the window.
  //
  // ⚠️ THE RULE: say what is happening, in plain words, and only when it changes what the user
  // should do. If everything is fine, say NOTHING — an empty banner takes no space.
  const bar = $("banner");
  const text = $("banner-text");
  const fill = $("banner-fill");
  const gauge = $("banner-bar");

  const show = (cls, msg, pct) => {
    bar.className = "show " + cls;
    text.textContent = msg;
    if (pct == null) {
      gauge.classList.remove("show");
    } else {
      gauge.classList.add("show");
      fill.style.width = Math.max(1, Math.min(100, pct)) + "%";
    }
  };

  const live = h.live || {};
  const working = h.indexing || live.running;
  const pct = live.percent != null ? live.percent : (h.embedding_percent || 0);

  if (working) {
    // ⚠️ "UPDATING", NOT "INDEXING". Indexing is our word; updating is what the user
    // experiences — their search catching up with their files.
    show("info",
         pct > 0 ? `Updating your search index — ${pct.toFixed(0)}%`
                 : "Updating your search index…",
         pct);
    // ⚠️ The ETA sits in the FOOTER with the document count. Two numbers in one 12px line makes
    // both of them unreadable.
    if (live.eta_seconds && $("activity")) {
      $("activity").textContent = `· about ${fmtEta(live.eta_seconds)} left`;
    }
    return;
  }
  if ($("activity")) $("activity").textContent = "";

  if (h.stale && h.stale.stale) {
    // ⚠️ THE APP REPAIRS ITSELF, SO THE MESSAGE SAYS SO RATHER THAN ASKING THE USER ANYTHING.
    // An index that is about to be fixed automatically is a different situation from one that
    // cannot be fixed, and only the second needs the user to care or to act.
    const rep = h.auto_repair || {};
    if (rep.enabled && rep.failures) {
      show("warn", "Some results may be out of date. The automatic fix is not working — "
                 + "details are in ~/.device-search/daemon.log", null);
    } else if (rep.enabled) {
      show("warn", "Some results may be out of date — updating shortly.", null);
    } else {
      show("warn", "Some results may be out of date.", null);
    }
    return;
  }

  // ⚠️ NOTHING TO SAY. A banner that is always present is one the user learns to stop reading.
  bar.className = "";
  text.textContent = "";
  gauge.classList.remove("show");
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
    let r = await fetch(`http://127.0.0.1:${PORT}${path}`, init);
    if (r.status === 401 && opts.auth !== false && !opts._retried) {
      // ⚠️⚠️ THE TOKEN CHANGES WHILE THE APP IS OPEN, AND THE UI CACHED IT.
      //
      // The daemon writes a NEW token every time it starts, deliberately — a leaked token is
      // worthless after a restart. But the UI reads it ONCE at boot, so the moment a daemon
      // starts (or restarts) the UI's copy is stale and every request returns 401
      // "bad or missing token" until the APP is restarted.
      //
      // ⚠️ That is exactly the reported symptom: "bad token" for a while, then it started
      // working — because a new daemon eventually matched the token the UI was holding.
      //
      // ⚠️ RE-READ AND RETRY ONCE, rather than making the token permanent. Persisting it would
      // fix this by giving up the property that makes a leaked token harmless.
      try {
        TOKEN = await invoke("read_token");
        const h2 = Object.assign({}, opts, { _retried: true });
        return await api(path, h2);
      } catch (e) { /* fall through to the original error */ }
    }
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
  // ⚠️ ask=1 ONLY WHEN THE TOGGLE IS ON. It costs an API call and uploads snippets, so it
  // must be an explicit choice every time rather than a mode the user forgot they enabled.
  const askOn = $("askbtn") && $("askbtn").classList.contains("on");
  const r = await api(`/api/search?q=${encodeURIComponent(q)}&k=25${askOn ? "&ask=1" : ""}`);
  if (!r.ok) { setStatus("err", r.error); results = []; render(); return; }
  // ⚠️ THE ANSWER IS KEPT SEPARATE FROM THE RESULTS, because it is a different kind of thing:
  // one is a list the user opens and checks, the other is a sentence that must be trusted or
  // verified. Merging them into one array would render them identically.
  answer = r.answer || null;
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

  // ⚠️⚠️ THE ANSWER RENDERS ABOVE THE LIST, AND THIS IS THE WHOLE POINT OF ANSWERING.
  //
  // It is what was asked for; the list is the evidence for it. ⚠️ AND THE EVIDENCE IS SHOWN
  // WITH IT — an answer without its sources is an assertion, and a user has no way to tell a
  // grounded answer from an invented one. The citations are clickable and the matching files
  // are in the list directly underneath, so checking the claim takes one glance.
  if (answer && answer.text) {
    const d = document.createElement("div");
    d.className = "answer" + (answer.uncited || (answer.invented || []).length ? " shaky" : "");
    const t = document.createElement("div");
    t.className = "answer-text";
    t.textContent = answer.text;
    d.appendChild(t);
    // ⚠️ EVERY DEGRADED STATE IS LABELLED. An answer that cites a source nobody sent, or
    // cites nothing at all, still LOOKS like a sourced answer — and that is the failure mode
    // that makes a generated answer worse than no answer.
    if ((answer.invented || []).length) {
      d.classList.add("bad");
      const w = document.createElement("div");
      w.className = "answer-flag";
      w.textContent = "⚠️ this answer cites sources that were never sent — treat it as unverified";
      d.appendChild(w);
    } else if (answer.uncited) {
      const w = document.createElement("div");
      w.className = "answer-flag";
      w.textContent = "⚠️ no citations — this is the model's wording, not evidence";
      d.appendChild(w);
    }
    for (const c of answer.citations || []) {
      const a = document.createElement("span");
      a.className = "answer-cite";
      a.textContent = `[${c.n}] ${c.path.split("/").pop()}`;
      a.title = c.path;
      a.onclick = () => openPath(c.path);
      d.appendChild(a);
    }
    box.appendChild(d);
  }

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
    // ⚠️ ESCAPE CLEARS FIRST, THEN HIDES. One keystroke that both erases the search and makes
    // the window disappear gives the user no way to edit a query they are halfway through.
    if ($("q").value) {
      $("q").value = ""; results = []; render(); setBanner("", "");
      return;
    }
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
// ⚠️⚠️ NOTHING IS CLEARED WHEN THE WINDOW HIDES.
//
// This handler used to wipe the query, the results AND the status line on every blur. The
// reasoning was "Spotlight starts fresh" — but Spotlight is a launcher and this is a FILE
// BROWSER. The actual workflow is: search, click a result, look at the file, come back. Wiping
// the query on every one of those round trips means retyping it, every time.
//
// ⚠️ And it destroyed errors as a side effect, so a failure vanished the moment the user
// looked away — which reads as "it fixed itself" and is how a bug survives for weeks.
//
// ⚠️ Clearing is now EXPLICIT: Escape empties it when the window is already focused.
document.addEventListener("visibilitychange", () => {
  // The query survives. Deliberately nothing here.
});

boot();
