// app.js — the interface logic. No framework, no build step.
//
// THE ONLY THING THIS FILE MUST GET RIGHT IS HONESTY ABOUT WHAT A RESULT MEANS.
// The backend deliberately returns two kinds of hit and does not let them look alike:
//
// lexical = true the user's words ARE in this file -> a finding
// lexical = false nearest in the embedding cone -> a lead, not a finding
//
// That distinction exists because it was MEASURED, not assumed: a query for a string that is
// not on disk scored 0.649 against a real query's 0.632. **The boundary is negative**, so no
// similarity threshold can separate them. If the UI rendered both lists the same way, it would
// assert exactly the thing the backend refuses to assert.

const $ = (id) => document.getElementById(id);
const invoke = window.__TAURI__?.core?.invoke;

let TOKEN = null;
let PORT = 8734;
let results = [];
let media = [];
let answer = null;
// WHETHER THE ROUTER CALLED THIS A QUESTION, AND WHY THERE IS NO ANSWER IF SO. Kept as
// state rather than decided at render time, because the server is the thing that classified it
// and a second opinion in the interface is a second source of truth.
let isQuestion = false;
let askReason = "";
// THE FILTER'S VERDICT, CARRIED IN STATE RATHER THAN READ FROM THE RESPONSE IN render().
//
// render() HAS NO `r`. It takes nothing and reads module state. The empty-filter block was
// inserted into render() while referencing `r` from run(), so it threw ReferenceError on
// every search and NOTHING WAS DRAWN AT ALL — no results, no message, no error. Reported by
// the user as "no search result list shows no matter what searched".
//
// A REFERENCE ERROR INSIDE A RENDER DOES NOT DEGRADE, IT BLANKS. And because the throw
// happened after the response arrived and before anything was drawn, every layer above it
// looked healthy: the daemon returned 10 results, the request succeeded, the console showed
// nothing a user would look at.
let metaInfo = null;
// Set when a fragment query ran without the code index, so the offer survives a
// redraw. Cleared by every search that does not need it.
let exactIndexOffer = null;
// Gaps that bear on the current search. Each carries the action that closes it,
// so the interface can offer a next step instead of an explanation.
let coverage = [];
// HOW MANY ARE DRAWN VS HOW MANY CAME BACK. Everything is RETURNED; this is how much is
// on screen, and it grows on request. The first version asked the server for 25 and the rest
// did not exist — the user could not reach them even by scrolling.
const PAGE = 40;
let shown = PAGE;
// WHETHER A MODEL IS CONNECTED, IN STATE RATHER THAN A run() LOCAL.
//
// It was declared inside run() and read inside render(), so every question-shaped search threw
// `Can't find variable: aiReady`. THE GUARD CAUGHT IT AND SAID SO — "The result list could
// not be drawn" — which is exactly why the guard went in, and the alternative was the blank
// list the user reported last time with no explanation at all.
//
// AND THE SAME MISTAKE TWICE IN A ROW IS THE POINT: moving one value (metaInfo) into state
// without checking its neighbours left the next one to fail the same way.
let aiReady = false;
// SET BY THE BUTTON, CONSUMED BY THE NEXT QUERY. See the note in run().
let wantsRerank = false; // a sentence the user will believe, unlike a list they can check // flattened, in display order
let wantsDeep = false; // one-shot, like wantsRerank
let scope = "all"; // which kinds to show; narrowing, not a mode
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
    // A MISSING TOKEN IS THE MOST LIKELY FIRST-RUN FAILURE, and the reason is almost always
    // "the daemon has never been started". Say that, rather than showing a stack trace.
    // NO TERMINAL COMMAND. If the app cannot start its own engine, that is the APP's failure
    // and the message should say what happened, not hand the user homework.
    setStatus("err", `The search engine could not be started (${e}).`);
    return;
  }
  // WAIT FOR THE ENGINE, DO NOT CHECK IT ONCE.
  //
  // The shell spawns the Python daemon and then immediately asks whether it is healthy. Starting
  // a Python process takes a moment to bind the port, so the FIRST check ALWAYS FAILS — and the
  // old code had no retry, so it displayed "the search daemon is not responding" permanently and
  // never looked again. The daemon was fine a second later and the user was told otherwise for
  // the rest of the session.
  //
  // AND THE MESSAGE WAS WRONG TOO. "Not responding" describes a crash. What was actually
  // happening is normal startup, and saying so is the difference between waiting and quitting.
  let h = await api("/api/health", { auth: false });
  if (!h.ok) {
    // MEASURE THE ELAPSED TIME, DO NOT USE THE LOOP COUNTER.
    // The old code printed `(${i}s)` where `i` was an ITERATION INDEX — and each iteration is a
    // 500ms sleep PLUS a failed fetch that has to time out, so it counted to 39 while claiming
    // 20 and the count was never seconds at all. A number labelled with the wrong unit is
    // worse than no number: it makes the user distrust every other figure on screen.
    const t0 = Date.now();
    // THREE MINUTES, NOT THIRTY SECONDS.
    //
    // Measured: with 866,000 documents indexed the daemon takes about 105 seconds to
    // answer its first request - the database is 9.7 GB. At thirty seconds the
    // interface declared failure at second 30 and the daemon answered at second 105,
    // so the user saw "did not start" on a machine where it started perfectly well.
    //
    // A timeout shorter than the thing it waits for is not a safety net. It is a
    // false alarm with a delay.
    while (!h.ok && Date.now() - t0 < 180000) {
      // SHOW THE REAL REASON WHILE WAITING. The retry loop used to print only a counter and
      // discard h.error — which is the string that says WHY, and it was computed every time and
      // thrown away. Diagnosing this took a macOS ATS investigation that one visible error
      // message would have short-circuited.
      setStatus("", `starting the search engine… ${((Date.now() - t0) / 1000).toFixed(0)}s` +
                    (h.error ? ` [${h.error}]` : ""));
      await new Promise((r) => setTimeout(r, 500));
      h = await api("/api/health", { auth: false });
    }
  }
  if (h.ok) {
    $("indexinfo").textContent =
      `${h.documents.toLocaleString()} documents indexed` +
      (h.semantic ? ` · ${h.semantic}` : " · semantic off");
    setStatus("", "");
    // THE BANNER IS DRIVEN BY THE DAEMON, NOT GUESSED BY THE UI. Only the engine knows which
    // documents are embedded and whether the vectors still describe them. A frontend computing
    // this itself would draw a confident progress bar attached to nothing.
    applyIndexState(h);
    // THE FIRST-RUN DECISION. An empty index with no message is what made the app look
    // broken: it launched, showed a search box that returned nothing, and gave no hint that a
    // terminal command was expected. If there is nothing to search, say so and offer the fix.
    if (h.documents === 0 || !h.documents) {
      Setup.open();
    } else if (h.indexing) {
      startPolling();
    }
  } else {
    // ONLY REACHED AFTER ~20 SECONDS OF RETRIES, so this now means what it says. It also gives
    // the command that produces the real error, because "not responding" alone is unactionable.
    // POINT AT THE LOG, NOT AT A COMMAND. The shell now writes the daemon's own output to
    // ~/.device-search/daemon.log, so the reason is in a file rather than lost to /dev/null.
    setStatus("err", `The search engine did not start. ` +
                     `${h.error ? h.error + ". " : ""}` +
                     `Details are in ~/.device-search/daemon.log`);
  }
  $("q").focus();

  // THE TOGGLE ONLY EXISTS WHEN IT CAN DO SOMETHING. A switch whose only possible
  // outcome is an error is worse than no switch: it teaches the user that the feature is
  // broken rather than that it is not set up.
  // THE PILL REFLECTS THE MODEL, AND IT IS ASKED RATHER THAN REMEMBERED. One source of
  // truth — /api/llm — because a cached copy is what let the folder list disagree with itself.
  const refreshAI = async () => {
    const pill = $("aipill");
    if (!pill) return;
    try {
      const lr = await api("/api/llm");
      const on = lr.ok && lr.configured;
      pill.style.display = on ? "" : "none";
      pill.title = on ? `using ${lr.model || "your model"} — click to change` : "";
    } catch (e) { pill.style.display = "none"; }
  };
  // AND IT MUST RETRY, BECAUSE AT PAGE LOAD THE DAEMON IS USUALLY NOT UP YET.
  //
  // This was called ONCE. The daemon takes tens of seconds to start — it loads the
  // embedding model first — so the very first /api/llm almost always fails, the catch hides
  // the pill, and NOTHING EVER ASKS AGAIN. The connection is fine; the check gave up.
  //
  // THE SAME BUG AS THE HEALTH CHECK THAT RAN AT t=0 AND NEVER AGAIN. A one-shot probe of
  // a service that starts asynchronously reports "not there" as a permanent fact.
  const retryAI = (n) => {
    refreshAI().then(() => {
      const pill = $("aipill");
      // STOP WHEN IT IS SHOWING, OR AFTER ~20 ATTEMPTS. Retrying forever on a machine with
      // no model configured would hammer an endpoint that is answering correctly.
      if (pill && pill.style.display !== "none") return;
      if (n > 0) setTimeout(() => retryAI(n - 1), 3000);
    });
  };
  retryAI(20);

  // AND EVERY TIME THE WINDOW REAPPEARS. This is a Spotlight-shaped window: it is hidden
  // and shown constantly, and the daemon may have started, stopped or been restarted in
  // between. A pill that is right once at launch and stale afterwards is worse than none,
  // because the user reads it as the current state.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshAI();
  });
  // AND WHENEVER THE MODEL PANEL CLOSES. Connecting a model must make the answer toggle
  // appear immediately; waiting for a relaunch reads as the connection having failed.
  window.addEventListener("ds:model-changed", refreshAI);
}

// ONE STATE OBJECT, TWO RENDERINGS. An in-progress index and a stale vector set are
// DIFFERENT problems — one resolves by waiting, the other does not — so they must not share a
// message. Showing "indexing…" for stale vectors would tell the user to wait for something that
// is not happening.
// A PLAIN SETTER, separate from applyIndexState, because these are messages the APP wants to
// show ("could not reach the daemon") rather than states the ENGINE reports.
function setBanner(cls, msg) {
  // WRITES TO #banner-text, NOT #banner. Setting textContent on the container DELETES the
  // progress bar element inside it, so the bar would disappear the first time any message was
  // set — a bug that only appears on the second banner.
  const bar = $("banner");
  const t = $("banner-text");
  if (!cls) { bar.className = ""; if (t) t.textContent = ""; return; }
  bar.className = "show " + cls;
  if (t) t.textContent = msg;
}

function applyIndexState(h) {
  // WHAT THIS SAYS, AND WHAT IT MUST NEVER SAY.
  //
  // The message it replaces read:
  //
  // "Semantic index is out of date — document count changed (5,573 -> 5,570); files were
  // modified. Re-run `ds index`. Exact and keyword matches are unaffected."
  //
  // FIVE THINGS WRONG WITH THAT, in order of how much they matter:
  // 1. It told the user to run a TERMINAL COMMAND. A shipped app must never do that.
  // 2. "semantic index", "document count changed", "exact and keyword matches" — someone
  // searching their own files does not know what those are and should not have to.
  // 3. It reported a NUMBER DIFFERENCE (5,573 -> 5,570) as if three were a quantity worth
  // reading. It is not. The user needs to know results might be wrong, not by how much.
  // 4. It implied the user had to act, when the app repairs itself automatically.
  // 5. It was three wrapped lines that pushed the results down the window.
  //
  // THE RULE: say what is happening, in plain words, and only when it changes what the user
  // should do. If everything is fine, say NOTHING — an empty banner takes no space.
  const bar = $("banner");
  const text = $("banner-text");
  const fill = $("banner-fill");
  const gauge = $("banner-bar");

  // THREE STATES, NOT TWO, AND THE THIRD IS THE ONE THE USER ASKED FOR.
  //
  // pct == null deliberately absent (not busy) -> no bar
  // pct == "moving" busy, but the total is NOT KNOWABLE -> INDETERMINATE bar
  // pct is a number the fraction is real -> measured bar
  //
  // A SCAN CANNOT HAVE A PERCENTAGE. The total is not knowable until the walk ends — that is
  // what a scan IS — and the previous attempt to show one produced "100%" from a DIFFERENT RUN
  // held for the whole crawl.
  //
  // BUT NO BAR AT ALL IS ALSO WRONG: a window that shows nothing moving, while a permission
  // prompt is up and the disk is being walked, reads as hung. The user's report was that there
  // was no way to tell whether indexing was finished.
  //
  // SO THE CRAWL GETS AN INDETERMINATE BAR — motion without a claim. It says "working"
  // without saying "this much", which is exactly what is known. macOS uses the same treatment
  // for the same reason.
  const show = (cls, msg, pct) => {
    bar.className = "show " + cls;
    text.textContent = msg;
    if (pct == null) {
      gauge.classList.remove("show", "moving");
      fill.style.width = "0%";
    } else if (pct === "moving") {
      gauge.classList.add("show", "moving");
      fill.style.width = "";
    } else {
      gauge.classList.remove("moving");
      gauge.classList.add("show");
      fill.style.width = Math.max(1, Math.min(100, pct)) + "%";
    }
  };

  const live = h.live || {};
  const working = h.indexing || live.running;

  // TELL THE SHELL, SO IT DOES NOT HIDE WHILE PERMISSION IS BEING ASKED FOR.
  // macOS shows the Desktop/Documents/Downloads prompts as SYSTEM MODALS, which take
  // focus; hide-on-blur then makes the window vanish behind the prompt and the app looks
  // like it crashed at the exact moment the user is granting it access.
  try {
    if (window.__TAURI__ && window.__TAURI__.core) {
      window.__TAURI__.core.invoke("set_busy", { busy: !!working });
    }
  } catch (e) { /* outside the shell: nothing to tell */ }
  // NO FALLBACK TO A STALE PERCENTAGE. THIS LINE WAS THE “STUCK AT 100%” BUG.
  //
  // It read: live.percent != null ? live.percent : (h.embedding_percent || 0)
  //
  // During a crawl the live percent is null — a scan has no knowable total — so it fell
  // back to `embedding_percent`, the value the PREVIOUS completed run left on disk. That is
  // 100. So the banner said “Updating your search index — 100%” for the entire time a
  // home directory was being walked.
  //
  // A PERCENTAGE FROM A DIFFERENT RUN IS NOT A PERCENTAGE. If the live value is absent the
  // honest thing is no bar, and a description of the phase instead.
  const crawling = live.stage === "crawling";
  const pct = crawling ? null : live.percent;

  if (working) {
    // "UPDATING", NOT "INDEXING". Indexing is our word; updating is what the user
    // experiences — their search catching up with their files.
    show("info",
         crawling
           ? `${live.note || "Looking through your files"}`
             + (live.files_seen != null ? ` — ${live.files_seen.toLocaleString()} found` : "")
             + (live.elapsed_seconds != null ? `, ${fmtEta(live.elapsed_seconds)} so far` : "")
             + (live.note ? "" : "…")
           : `Reading meaning into your files — ${(pct || 0).toFixed(0)}%`
             + (live.elapsed_seconds != null ? `, ${fmtEta(live.elapsed_seconds)} so far` : ""),
         // "moving" DURING A SCAN, THE NUMBER DURING EMBEDDING. Passing `pct` here — which
         // is null while crawling — is why the bar was still absent after the first attempt.
         crawling ? "moving" : pct);

    // The ETA sits in the FOOTER with the document count. Two numbers in one 12px line makes
    // both of them unreadable.
    if (live.eta_seconds && $("activity")) {
      $("activity").textContent = `· about ${fmtEta(live.eta_seconds)} left`;
    }
    return;
  }
  if ($("activity")) $("activity").textContent = "";

  if (h.stale && h.stale.stale) {
    const reason = (h.stale.reason || "").trim();
    const rep = h.auto_repair || {};
    const sev = h.stale.severity || "";

    // A COVERAGE GAP IS NOT A FAULT, AND IT MUST NOT NAG FOREVER.
    //
    // The banner used to say "Some results may be out of date." on every poll, for
    // ever, and ignored h.stale.reason entirely. On this machine the reason is that
    // meaning-search covers 4,488 of 865,519 documents - a fact about how much was
    // ever embedded, not a fault that will clear. A warning that never clears is
    // one the user learns to stop reading, which is how a REAL one gets missed.
    //
    // So: say what is actually true, and let it be put away. The dismissal is per
    // session and per reason, so a different staleness shows up again.
    if (h.stale.severity === "outdated" && reason) {
      if (sessionStorage.getItem("staleDismissed") === reason) {
        bar.className = "";
        return;
      }
      // The message is its own element and the dismissal is a separate one, so the
      // action does not read as the last three words of the sentence.
      show("warn", reason, null);
      const txt = document.getElementById("banner-text");
      if (txt) {
        // THE MESSAGE SAID WHAT WAS WRONG AND NOTHING ABOUT WHAT TO DO.
        //
        // A coverage gap is presented as a problem, so it has to carry the way out of
        // it. The button opens Settings on the panel that closes the gap. The
        // dismissal stays, because a warning that cannot be put away is one the user
        // learns to ignore, and then a real one is missed.
        const fix = document.createElement("span");
        fix.textContent = " Index it";
        fix.style.cssText = "margin-left:10px; font-weight:600; cursor:pointer;"
                          + " text-decoration:underline; text-underline-offset:2px;"
                          + " white-space:nowrap;";
        fix.onclick = (ev) => {
          ev.stopPropagation();          // the banner itself dismisses on click
          Setup.open("ai");
        };
        txt.appendChild(fix);

        const x = document.createElement("span");
        x.textContent = " Dismiss";
        x.style.cssText = "margin-left:10px; opacity:.65; text-decoration:underline;"
                        + " text-underline-offset:2px; white-space:nowrap;";
        txt.appendChild(x);
      }
      const b = document.getElementById("banner");
      if (b) {
        b.style.cursor = "pointer";
        b.title = "click to hide this for now";
        b.onclick = () => {
          sessionStorage.setItem("staleDismissed", reason);
          bar.className = "";
        };
      }
      return;
    }

    if (rep.enabled && rep.failures) {
      show("warn", "Some results may be out of date. The automatic fix is not working — "
                 + "details are in ~/.device-search/daemon.log", null);
    } else if (rep.enabled) {
      show("warn", "Some results may be out of date — updating shortly.", null);
    } else {
      show("warn", reason || "Some results may be out of date.", null);
    }
    return;
  }

  // NOTHING TO SAY. A banner that is always present is one the user learns to stop reading.
  bar.className = "";
  text.textContent = "";
  gauge.classList.remove("show");
}

// POLLED WHILE INDEXING, and the poll stops when it stops. A permanent 2-second timer in a
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
    // The Content-Type matters: without it the server's json.loads gets an empty body, and
    // the request looks like it arrived with no data rather than being malformed.
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  try {
    let r = await fetch(`http://127.0.0.1:${PORT}${path}`, init);
    if (r.status === 401 && opts.auth !== false && !opts._retried) {
      // THE TOKEN CHANGES WHILE THE APP IS OPEN, AND THE UI CACHED IT.
      //
      // The daemon writes a NEW token every time it starts, deliberately — a leaked token is
      // worthless after a restart. But the UI reads it ONCE at boot, so the moment a daemon
      // starts (or restarts) the UI's copy is stale and every request returns 401
      // "bad or missing token" until the APP is restarted.
      //
      // That is exactly the reported symptom: "bad token" for a while, then it started
      // working — because a new daemon eventually matched the token the UI was holding.
      //
      // RE-READ AND RETRY ONCE, rather than making the token permanent. Persisting it would
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
    // A NETWORK ERROR AND AN EMPTY RESULT SET MUST NOT LOOK THE SAME. A search that failed
    // and a search that found nothing are different facts; showing an empty list for both is
    // how a user concludes their file does not exist.
    return { ok: false, error: `daemon unreachable (${e.message})` };
  }
}

// ---------------------------------------------------------------- search
// ONE PLACE THAT FORGETS A SEARCH.
//
// Clearing the input used to reset `results` alone. Everything else - the answer,
// the media tier, the metadata note, the coverage lines, the code-index offer -
// rendered from state that was still there, so emptying the box left a panel full
// of results for a query that no longer existed.
//
// Selection is cleared too: the arrow keys act on the list, and an index pointing
// into a list that is gone is an error waiting for a keystroke.
function clearResults() {
  results = [];
  media = [];
  answer = null;
  metaInfo = null;
  exactIndexOffer = null;
  coverage = [];
  sel = -1;
}

function onType() {
  clearTimeout(timer);
  const q = $("q").value.trim();
  if (!q) { clearResults(); render(); setStatus("", ""); return; }
  // DEBOUNCED, but short. 120ms is below the threshold where typing feels laggy, and the
  // daemon answers in ~60ms so the result arrives while the user is still typing.
  timer = setTimeout(() => run(q), 120);
}

async function run(q) {
  // ask=1 ONLY WHEN THE TOGGLE IS ON. It costs an API call and uploads snippets, so it
  // must be an explicit choice every time rather than a mode the user forgot they enabled.
  // NO TOGGLE. Ask whenever a model is connected and let the SERVER decide whether the
  // query is a question — "how does the autoloader work" is one, "RetryMiddleware" is not, and
  // asking the user to classify their own input before typing it is the opposite of doing the
  // least work. Results come back either way, so a wrong answer is checkable in a glance.
  aiReady = AI.state().configured;
  // `wantsRerank` IS A ONE-SHOT FLAG. Clicking the button sets it, this query consumes it
  // and clears it — so the next keystroke is fast again. A sticky mode would silently add five
  // seconds to every search from then on, and the user would blame the app rather than the mode.
  const rr = wantsRerank ? "&rerank=1" : "";
  wantsRerank = false;
  // Same one-shot pattern as re-ranking: consumed by this query and
  // cleared, so the next keystroke goes back to the fast path.
  const dd = wantsDeep ? "&deep=1" : "";
  wantsDeep = false;
  if (dd) setStatus("", "searching deeper…");
  if (rr) setStatus("", "re-ranking…");
  // SHOW THAT WORK IS HAPPENING, FROM THE MOMENT THE KEYSTROKE LANDS.
  // A search that takes two seconds with no feedback reads as broken, and the user presses
  // enter again — which is how a slow search becomes a busy one.
  if ($("busy-overlay")) $("busy-overlay").classList.add("on");
  const r = await api(`/api/search?q=${encodeURIComponent(q)}&k=25${aiReady ? "&ask=1" : ""}${rr}${dd}&scope=${encodeURIComponent(scope)}`);
  if (!r.ok) { setStatus("err", r.error); results = []; render(); return; }
  // THE ANSWER IS KEPT SEPARATE FROM THE RESULTS, because it is a different kind of thing:
  // one is a list the user opens and checks, the other is a sentence that must be trusted or
  // verified. Merging them into one array would render them identically.
  media = r.media || [];
  answer = r.answer || null;
  metaInfo = r.meta || null;
  exactIndexOffer = r.exact_index && r.exact_index.needed ? r.exact_index : null;
  // Asked separately so a slow coverage check cannot delay the results. It is
  // replaced when it arrives and the list is redrawn once.
  coverage = [];
  (async () => {
    try {
      const cov = await api(`/api/coverage?q=${encodeURIComponent(q)}`
                          + `&kind=${encodeURIComponent(r.kind || "")}`);
      if (cov.ok && Array.isArray(cov.gaps)) {
        coverage = cov.gaps;
        if (coverage.length) render();
      }
    } catch (_) { /* coverage is an extra, never a failure */ }
  })();
  isQuestion = r.kind === "question";
  // RE-RANKING CANNOT ADD A RESULT, ONLY REORDER ONE. Saying "0 moved" is not a failure — it
  // means the first pass was already right, and a user who clicked and saw the same list
  // deserves to know that rather than assume it did nothing.
  if (r.rerank) {
    if (r.rerank.error) setStatus("err", `Re-ranking failed: ${r.rerank.error}`);
    else setStatus("", `re-ranked ${r.rerank.top_n} results in ${r.rerank.seconds.toFixed(1)}s`
                     + ` — ${r.rerank.moved} changed position`);
  }
  // THE REASON, IN THE USER'S WORDS. Every branch names what to DO about it — a
  // explanation without a next step is only slightly better than silence.
  if (isQuestion && !(answer && answer.text)) {
    if (r.answer_error) {
      askReason = `Could not get an answer: ${r.answer_error}`;
    } else if (!aiReady) {
      // "AI", NOT "A MODEL". "Model" is the vocabulary of the thing you are building, not
      // the thing the user bought. They connected an AI; the setting is called AI; the footer
      // says AI. A message that names the feature differently from the button that turns it
      // on makes the user look for something that does not exist under that name.
      askReason = "This looks like a question — connect AI to answer better"
    } else {
      // THE MOST COMMON CASE, AND THE ONE THAT LOOKED BROKEN. A question like "how many
      // folders are in Desktop?" asks for a COUNT, and no passage in any file states a count —
      // so extraction from text cannot produce it. Saying so is the whole fix.
      askReason = "I could not answer that from the text of your files. Counting and "
                + "arithmetic are not things this can do — it quotes what files say. "
                + "The results below are what matched.";
    }
  } else if (!isQuestion) {
    askReason = "";
  }
  // Ignore a response that arrived for a query the user has already moved past. Without
  // this, a slow response overwrites a fast one and the list shows results for an older query.
  if ($("q").value.trim() !== q) return;

  results = r.results.map((x) => ({ ...x, group: x.lexical ? "match" : "meaning" }));
  shown = PAGE; // a new query starts at the top of its own results
  sel = results.findIndex((x) => x.lexical);
  if (sel < 0) sel = 0;

  const parts = [`${r.took_ms}ms`, `routed as ${r.kind}`];
  if (r.broadened) parts.push("broadened after an empty first pass");
  if (r.llm && r.llm.blocked) parts.push(`${r.llm.blocked} snippet(s) blocked by the secret interlock`);
  if ($("busy-overlay")) $("busy-overlay").classList.remove("on");
  setStatus("", parts.join(" · "));
  // SHOWN ONLY WHEN THERE IS SOMETHING TO RE-RANK, AND BESIDE WHAT IT ACTS ON.
  //
  // It was a footer link: always visible, and silent about what it would reorder. A button
  // that does nothing on an empty result list teaches the user it does nothing.
  //
  // AND IT IS RIGHT-ALIGNED TO THE TRACE, because it is an action ON those results — the
  // trace says how they were found, this says how to order them better.
  if ($("rerank")) $("rerank").style.display = results.length ? "" : "none";
  render();
  $("q").focus();
}

// ---------------------------------------------------------------- render
function render() {
  const box = $("results");
  box.textContent = "";
  // THE WHOLE BODY RUNS INSIDE A GUARD, AND THIS IS THE POINT OF THE GUARD.
  //
  // A search returned 10 results from the daemon, the request succeeded, and the user saw an
  // EMPTY LIST WITH NO ERROR. A ReferenceError inside render() blanks the interface and looks
  // exactly like "there are no results" — the failure and the empty state are the same pixels,
  // which is the same class of bug as the silent crawl and the self-erasing message.
  //
  // A RENDER THAT CANNOT COMPLETE MUST SAY SO. Swallowing the error would hide it; drawing
  // nothing while claiming success is what it was already doing.
  try {
    renderInner();
  } catch (e) {
    box.textContent = "";
    const d = document.createElement("div");
    d.className = "answer bad";
    d.textContent = `The result list could not be drawn: ${e && e.message ? e.message : e}`;
    box.appendChild(d);
    // AND THE DETAIL GOES WHERE A DEVELOPER WILL SEE IT. The panel says what broke; the
    // console says where. A message the user cannot act on is still better than a blank list.
    console.error("render() failed:", e);
  }
}



function renderInner() {
  const box = $("results");
  box.textContent = "";

  // A FAINT LINE IN THE SPACE WHERE RESULTS GO.
  //
  // This block used to sit at MODULE level, after render() - so it appended to the
  // results container once at load, landed below the footer in the document flow,
  // and never disappeared when a search returned anything. It belongs here, inside
  // the function that owns the results area and runs on every outcome.
  //
  // One quiet line, not a heading, a paragraph and three bordered buttons. The
  // earlier version competed with the results it was describing.
  // The offer, when a fragment query ran without the code index.
  //
  // Above the results rather than buried in Settings, because this is the moment it
  // becomes relevant. What it replaces was a silent twenty-second scan.
  if (exactIndexOffer) {
    const offer = document.createElement("div");
    offer.className = "index-offer";
    const say = document.createElement("span");
    say.textContent = "Finding exact fragments needs the code index. Search runs "
                    + "without it, so a query like this is inexact and may miss files.";
    offer.appendChild(say);

    const go = document.createElement("button");
    go.className = "index-offer-btn";
    go.textContent = "See the cost";
    go.onclick = async () => {
      go.disabled = true;
      go.textContent = "Checking\u2026";
      const c = await api("/api/deep-index");
      if (!c.ok) { go.disabled = false; go.textContent = "See the cost"; return; }
      go.remove();
      const line = document.createElement("span");
      line.className = "index-offer-cost";
      line.textContent = c.built
        ? "Already built."
        : `${c.documents.toLocaleString()} documents \u00b7 about ${c.estimated_gb} GB \u00b7 `
          + `about ${c.estimated_minutes} minutes. You have ${c.free_gb} GB free.`;
      offer.appendChild(line);
      if (!c.built && c.estimated_gb <= c.free_gb) {
        const build = document.createElement("button");
        build.className = "index-offer-btn";
        build.textContent = "Build it";
        build.onclick = async () => {
          build.disabled = true;
          build.textContent = "Starting\u2026";
          const b = await api("/api/deep-index", { method: "POST", body: {} });
          if (!b.ok) { build.disabled = false; build.textContent = "Build it"; return; }
          offer.textContent = "Building in the background. Search keeps working; "
                            + "progress appears in the banner.";
        };
        offer.appendChild(build);
      }
    };
    offer.appendChild(go);
    box.appendChild(offer);
  }
  // WHAT IS NOT SEARCHABLE, AND WHERE TO FIX IT.
  //
  // At most two lines, because a list of four gaps above the results is a lecture.
  // Each ends in a button that opens Settings on the panel that closes it - the
  // message says what is missing and the button says what to do about it, so the
  // user is never left to work out the next step alone.
  if (coverage.length) {
    const box2 = document.createElement("div");
    box2.className = "coverage";
    for (const gap of coverage.slice(0, 2)) {
      const line = document.createElement("div");
      line.className = "coverage-line";

      const text = document.createElement("span");
      text.className = "coverage-text";
      text.textContent = gap.detail;
      if (gap.why) text.title = gap.why;   // the reason, without spending the line on it
      line.appendChild(text);

      if (gap.action) {
        const go = document.createElement("button");
        go.className = "coverage-btn";
        go.textContent = gap.action.label;
        go.onclick = () => Setup.open(gap.action.panel);
        line.appendChild(go);
      }
      box2.appendChild(line);
    }
    box.appendChild(box2);
  }
  if (!results.length && !answer && !($("q") && $("q").value.trim())) {
    const hint = document.createElement("div");
    hint.className = "empty-hint";
    hint.innerHTML = "Search files, PDFs, screenshots, video and audio"
                   + "<span>by text, by meaning, or by describing what you are looking for</span>";
    box.appendChild(hint);
  }


  // THE SPINNER IS CLEARED HERE, NOT ONLY ON THE SUCCESS PATH. render() runs on every
  // outcome — results, an error, an empty filter — so this is the one place that cannot miss a
  // branch. A spinner that never stops is worse than none: it says "working" forever.
  if ($("busy-overlay")) $("busy-overlay").classList.remove("on");

  // THE RE-RANK BUTTON'S VISIBILITY IS DECIDED HERE AND NOWHERE ELSE.
  //
  // It was set in the success path of run(), which means every OTHER way the list can become
  // empty — an error, a cleared box, a failed request — would leave it on screen offering to
  // reorder nothing. One place that draws, one place that decides.
  if ($("rerank")) $("rerank").style.display = results.length ? "" : "none";

  // THE ANSWER RENDERS ABOVE THE LIST, AND THIS IS THE WHOLE POINT OF ANSWERING.
  //
  // It is what was asked for; the list is the evidence for it. AND THE EVIDENCE IS SHOWN
  // WITH IT — an answer without its sources is an assertion, and a user has no way to tell a
  // grounded answer from an invented one. The citations are clickable and the matching files
  // are in the list directly underneath, so checking the claim takes one glance.
  // A QUESTION MUST ALWAYS PRODUCE SOMETHING, INCLUDING "I CANNOT TELL YOU".
  //
  // The user asked "how many folders are in Desktop?", saw the label "routed as question" and a
  // list of files, and had no idea why there was no answer. FROM THEIR SIDE THOSE ARE THE
  // SAME PIXELS as a working search, so the app looked broken without saying it was.
  //
  // EVERY REASON GETS A SENTENCE, because they need different actions:
  // no model connected -> connect one in Settings
  // the model refused -> ask something the files can answer
  // the request failed -> the reason
  // a question it cannot answer, like a COUNT, is the most common case of all
  // "NO RESULTS" AND "NOTHING MATCHES THAT FILTER" ARE DIFFERENT FACTS.
  //
  // The user searched "10gb files", saw nothing, and could not tell whether the app had failed
  // or whether they genuinely have no files that large. An empty list is only an answer when
  // the app says WHAT it looked for.
  if (metaInfo && metaInfo.empty_is_the_answer) {
    const d = document.createElement("div");
    d.className = "answer note";
    const t = document.createElement("div");
    t.className = "answer-text";
    // SCOPE MATTERS: THIS INDEXES WHAT THE USER CHOSE, NOT THE WHOLE COMPUTER.
    // "there is no file on this Mac" was wrong twice over. It claims a fact about the
    // entire machine when only the selected folders were scanned — false, and it answers a
    // question nobody asked. And it names a platform, in an application meant to run on
    // Windows and Linux too.
    //
    // AND IT NO LONGER EXPLAINS ITSELF. "That is the answer, not an error" is the app
    // reassuring the user about its own health. They wanted a fact, not a disclaimer.
    // THE BACKEND'S REASON WHEN THERE IS ONE, A GENERIC LINE WHEN THERE IS NOT.
    //
    // The generic line was stating a fact the app had no basis for: "no file matches size
    // 10 GB to 11 GB" for an index that holds NOTHING above 2 MB. “I found nothing” and “I did
    // not look” are different answers, and only the backend knows which one it gave.
    t.textContent = metaInfo.empty_reason
      || `No file matches ${metaInfo.explain || "that filter"} in the folders being searched. `
         + `Widen the search area in Settings if you expected one.`;
    d.appendChild(t);
    box.appendChild(d);
  }
  if (isQuestion && !(answer && answer.text)) {
    const d = document.createElement("div");
    d.className = "answer note";
    const t = document.createElement("div");
    t.className = "answer-text";
    t.textContent = askReason;
    d.appendChild(t);
    // AND A BUTTON, BECAUSE THE MESSAGE NAMES A FIX THE USER CANNOT REACH FROM HERE.
    //
    // "Connect AI" is an instruction. An instruction the user has to translate into "which
    // footer link, which tab" before they can act on it is work the app should have done.
    // It opens Settings DIRECTLY ON THE AI STEP, because that is where the fix is and
    // landing on folders would make them find it again.
    if (!aiReady) {
      const b = document.createElement("button");
      b.className = "answer-cta";
      b.textContent = "Connect AI";
      b.onclick = () => Setup.open("ai");
      d.appendChild(b);
    }
    box.appendChild(d);
  }

  if (answer && answer.text) {
    const d = document.createElement("div");
    d.className = "answer" + (answer.uncited || (answer.invented || []).length ? " shaky" : "");
    const t = document.createElement("div");
    t.className = "answer-text";
    t.textContent = answer.text;
    d.appendChild(t);
    // EVERY DEGRADED STATE IS LABELLED. An answer that cites a source nobody sent, or
    // cites nothing at all, still LOOKS like a sourced answer — and that is the failure mode
    // that makes a generated answer worse than no answer.
    if ((answer.invented || []).length) {
      d.classList.add("bad");
      const w = document.createElement("div");
      w.className = "answer-flag";
      w.textContent = "this answer cites sources that were never sent — treat it as unverified";
      d.appendChild(w);
    } else if (answer.uncited) {
      const w = document.createElement("div");
      w.className = "answer-flag";
      w.textContent = "no citations — this is the model's wording, not evidence";
      d.appendChild(w);
    }
    // What the answer had to work with, shown only when it is tight.
    if (answer.context && answer.context.budget) {
      const cx = answer.context;
      if (cx.tokens_used / cx.budget > 0.9 || cx.truncated > 0) {
        const n2 = document.createElement("span");
        n2.className = "answer-flag";
        n2.textContent = `context ${cx.tokens_used.toLocaleString()}`
          + `/${cx.budget.toLocaleString()} tokens across ${cx.sources} source`
          + `${cx.sources === 1 ? "" : "s"}`
          + (cx.truncated ? ` — ${cx.truncated} truncated` : "");
        d.appendChild(n2);
      }
    }
    for (const c of answer.citations || []) {
      const a = document.createElement("span");
      a.className = "answer-cite";
        // The page, when the source is a PDF: pdftext leaves [page N] markers
        // in the extracted text, so the backend can report which pages a
        // snippet came from. Without it a citation says "this file" when the
        // file has 40 pages, which is the same as not citing it.
        let where = "";
        if (c.page_first) {
          where = c.page_last && c.page_last !== c.page_first
            ? ` p.${c.page_first}–${c.page_last}`
            : ` p.${c.page_first}`;
        }
        a.textContent = `[${c.n}] ${c.path.split("/").pop()}${where}`;
        a.title = c.page_first ? `${c.path} (page ${c.page_first})` : c.path;
      a.onclick = () => openPath(c.path);
      d.appendChild(a);
    }
    box.appendChild(d);
  }

  // PICTURES AND VIDEO, AS THEIR OWN SECTION.
  //
  // Not merged into the list below: those rows mean "this file contains what you
  // typed", these mean "this picture looks like what you described". One numbered
  // list would imply one ranking, and BM25 and cosine are not on the same scale.
  if (media && media.length) {
    const mh = document.createElement("div");
    mh.className = "group-head";
    mh.textContent = "LOOKS LIKE THIS";
    box.appendChild(mh);

    for (const m of media) {
      const row = document.createElement("div");
      row.className = "media-row";

      const kind = document.createElement("span");
      kind.className = "media-kind";
      kind.textContent = m.kind === "video" ? "VIDEO" : "IMAGE";

      const name = document.createElement("span");
      name.className = "media-name";
      name.textContent = m.path.split("/").pop();

      row.appendChild(kind);
      row.appendChild(name);

      // A video hit is a MOMENT, not a file. "2:50" is the difference between a
      // citation and a scavenger hunt.
      if (m.at) {
        const at = document.createElement("span");
        at.className = "media-at";
        at.textContent = m.at;
        row.appendChild(at);
      }

      row.title = m.at ? `${m.path} (at ${m.at})` : m.path;
      row.onclick = () => openPath(m.path);
      box.appendChild(row);
    }
  }

  // A WAY FORWARD WHEN THERE IS NOTHING, OR ALMOST NOTHING.
  //
  // A miss used to be final: the list was empty and the only thing on screen was
  // an explanation. There is always one more thing to try - widen the query to its
  // two rarest words - and the user should not have to guess the right words
  // themselves to get it.
  //
  // Offered when the result list is thin rather than only when it is empty: three
  // results for a five-word query is often a near miss, and the difference between
  // "nothing" and "something close" is not visible in a count.
  if (results.length < 5) {
    const deeper = document.createElement("button");
    deeper.className = "deeper-btn";
    deeper.textContent = "Search deeper";
    deeper.title = "Widen the query and include pictures and video";
    deeper.onclick = () => {
      // Dispatch the same event the search box emits, rather than calling the
      // search function directly. `run` is not in scope here - it lives inside the
      // closure that owns the input handler - and reaching for it produced a call
      // to a name that does not exist, which fails only when the button is
      // clicked. The event goes through the existing debounce and state handling
      // instead of duplicating it.
      wantsDeep = true;
      $("q").dispatchEvent(new Event("input", { bubbles: true }));
    };
    box.appendChild(deeper);
  }


  if (!results.length) return;

  // A PAGE, NOT A TRUNCATION. The user asked to see every result and to have pagination
  // rather than a cut — and both halves matter. Drawing 5,000 rows at once would freeze the
  // window, which is a WORSE way of hiding them than a cutoff, because it looks like a hang.
  const _page = results.slice(0, shown);

  let lastGroup = null;
  for (let i = 0; i < _page.length; i++) {
    const r = _page[i];
    if (r.group !== lastGroup) {
      lastGroup = r.group;
      const h = document.createElement("div");
      h.className = "group" + (r.group === "meaning" ? " meaning" : "");
      h.textContent = r.group === "match" ? "Matches — your words are in these files"
                                          : "Closest by meaning — no word match";
      if (r.group === "meaning") {
        // THE DISCLAIMER IS PART OF THE UI, NOT A COMMENT IN THE CODE. A user who does not
        // read the source has no other way to learn that these are leads rather than findings.
        const n = document.createElement("span");
        n.className = "note";
        n.textContent = "Not evidence the thing exists — nearest in the index, nothing more.";
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
    // OCR IS MARKED. A hit found inside a screenshot by OCR is a weaker claim than one read
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

  // AND THE REST ARE ONE CLICK AWAY, WITH THE NUMBER STATED. A silent stop at 40 is
  // indistinguishable from "there are only 40" — which is why this says how many are left
  // rather than just offering to load more.
  if (results.length > shown) {
    const more = document.createElement("div");
    more.className = "more";
    more.textContent = `Show ${Math.min(PAGE, results.length - shown)} more `
                     + `(${results.length - shown} of ${results.length} not shown)`;
    more.onclick = () => { shown += PAGE; render(); };
    box.appendChild(more);
  }
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
  // OPENING IS DELEGATED TO THE SHELL. A webview cannot launch a file manager; the Rust side
  // can. And the path is taken from `full_path`, never reconstructed from the display path —
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
  // Rounded to something a person would say. "4h 12m" is actionable; "15120.4s" is not.
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return m ? `${h}h ${m}m` : `${h}h`;
}

// ---------------------------------------------------------------- events
$("q").addEventListener("input", onType);
window.addEventListener("keydown", (e) => {
  // SETUP MUST BE REACHABLE AFTER FIRST RUN. A one-shot wizard that cannot be reopened makes
  // "add another folder" impossible without deleting the index.
  if (e.key === "," && (e.metaKey || e.ctrlKey)) { e.preventDefault(); Setup.open(); return; }
  // ⌘. FOR THE MODEL PANEL. ⌘, is folders; the two are different decisions and get
  // different keys rather than one panel that does both.
  // ⌘. GOES STRAIGHT TO THE MODEL STEP — the shortcut you press when you already know
  // what you want to change.
  if (e.key === "." && (e.metaKey || e.ctrlKey)) {
    e.preventDefault(); Setup.open("ai"); return;
  }
  if (e.key === "Escape") {
    e.preventDefault();
    if (Setup.isOpen()) { Setup.close(); return; }
    if (Setup.isOpen()) { Setup.close(); return; }
    // ESCAPE CLEARS FIRST, THEN HIDES. One keystroke that both erases the search and makes
    // the window disappear gives the user no way to edit a query they are halfway through.
    if ($("q").value) {
      $("q").value = ""; clearResults(); render(); setBanner("", "");
      return;
    }
    hideWindow();
  }
  else if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
  else if (e.key === "Enter") { e.preventDefault(); open(e.metaKey || e.ctrlKey); }
});
// The OS focus and the DOM focus are separate. Showing the window does not put the cursor in
// the box, so the shell emits an event and the box claims focus here.
if (window.__TAURI__?.event) {
  window.__TAURI__.event.listen("focus-input", () => $("q").select());
}
// THE SETTINGS AFFORDANCE, because a first-run wizard that cannot be reopened makes "add
// another folder" impossible without deleting the index.
$("settings").addEventListener("click", () => Setup.open());
// THE PILL IS A DOOR, NOT A LABEL. Clicking it opens Settings on the model step, so
// "is this using my key?" is one click to check and one click to change.
if ($("aipill")) $("aipill").addEventListener("click", () => Setup.open("ai"));
// ⌘⇧R, BECAUSE A BUTTON YOU CLICK AFTER EVERY SEARCH IS STILL A TRIP TO THE MOUSE.
// The shortcut is where a user who uses this often will end up.
window.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key.toLowerCase() === "r") {
    e.preventDefault();
    const q = $("q").value.trim();
    if (q && results.length) { wantsRerank = true; run(q); }
  }
});
if ($("rerank")) $("rerank").addEventListener("click", () => {
  const q = $("q").value.trim();
  if (!q) return;
  wantsRerank = true;
  run(q);
});

// Clear when hidden. A search box that reopens showing the previous query makes the user
// select-and-delete every time; Spotlight starts empty.
// NOTHING IS CLEARED WHEN THE WINDOW HIDES.
//
// This handler used to wipe the query, the results AND the status line on every blur. The
// reasoning was "Spotlight starts fresh" — but Spotlight is a launcher and this is a FILE
// BROWSER. The actual workflow is: search, click a result, look at the file, come back. Wiping
// the query on every one of those round trips means retyping it, every time.
//
// And it destroyed errors as a side effect, so a failure vanished the moment the user
// looked away — which reads as "it fixed itself" and is how a bug survives for weeks.
//
// Clearing is now EXPLICIT: Escape empties it when the window is already focused.
document.addEventListener("visibilitychange", () => {
  // The query survives. Deliberately nothing here.
});

boot();

// ---- scope chips --------------------------------------------------------
//
// Shown only when a media index exists. A "Pictures" filter on a machine with no
// indexed images is a button that always returns nothing, which teaches the user
// that the feature is broken rather than that it is empty.
(async function initScopes() {
  const bar = document.getElementById("scopes");
  if (!bar) return;

  try {
    const r = await api("/api/media");
    const d = (r && r.data) || {};
    const hasMedia = Boolean(d.image_index_built || d.video_index_built);
    if (!hasMedia) return; // stay hidden: nothing to filter
    bar.style.display = "";
    // Hide the individual chips whose index does not exist, so "Video" is not
    // offered before any video has been indexed.
    for (const b of bar.querySelectorAll(".scope")) {
      const sc = b.dataset.scope;
      if (sc === "image" && !d.image_index_built) b.style.display = "none";
      if ((sc === "video" || sc === "audio") && !d.video_index_built) b.style.display = "none";
    }
  } catch (e) {
    return;
  }

  for (const b of bar.querySelectorAll(".scope")) {
    b.onclick = () => {
      scope = b.dataset.scope;
      for (const o of bar.querySelectorAll(".scope")) o.classList.toggle("on", o === b);
      // Re-run the current query under the new scope. Dispatching the input event
      // rather than calling the search function keeps one code path for state.
      const q = document.getElementById("q");
      if (q && q.value.trim()) q.dispatchEvent(new Event("input", { bubbles: true }));
    };
  }
})();
