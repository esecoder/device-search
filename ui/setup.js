// setup.js — the first-run screen, so the app is usable without a terminal.
//
// ⚠️⚠️ WHY THIS EXISTS AND WHY IT IS NOT A NICETY.
// The app used to do exactly nothing on first launch against an empty index: no message, no
// button, no hint that a terminal command was required. **"Launch it and run `ds index`" is a
// developer workflow shipped to a user**, and the user's reaction — "so what am I supposed to
// do?" — is the correct one.
//
// ⚠️ THE STATE MACHINE, and every state is reachable:
//
//     no roots, no documents    -> SETUP      (pick folders, press Index)
//     indexing in progress      -> PROGRESS   (bar, ETA, search still works on what exists)
//     documents, not indexing   -> SEARCH     (the normal state)
//     empty query               -> SEARCH     (shows what is indexed)
//
// ⚠️ SEARCH IS NEVER DISABLED. During indexing the results are partial, and saying so is
// better than hiding the box — a user who can search 40% of their disk is better served than
// one watching a progress bar with no way to do anything.

const Setup = (() => {
  let roots = { configured: [], indexed: [], suggested: [] };
  let chosen = new Set();
  // ⚠️ PATHS THE USER ADDED BY HAND, kept SEPARATE FROM THE SERVER'S SUGGESTIONS. render() needs
  // to know which rows are "options you were offered" and which are "things you added", because
  // only the second can be removed.
  let extras = new Set();
  let busy = false;
  // ⚠️ THE WHOLE-DEVICE CHOICE, CARRIED THROUGH TO THE API. `POST /api/roots` takes a mode,
  // and the backend has always understood "everything" — this is only the interface catching up.
  let wholeDevice = false;
  // ⚠️⚠️ THE MESSAGE IS STATE, NOT A DOM WRITE, AND THIS IS THE THIRD TIME TODAY.
  //
  // note() wrote straight into #setup-msg. But render() begins with `host.textContent = ""`,
  // so every sequence of
  //
  //     note("something failed");  render();
  //
  // printed the message and then DELETED IT. Which is exactly the bug that made picked folders
  // vanish — render() wiping DOM that held state — and I fixed that one without looking for
  // the same shape two functions away.
  //
  // ⚠️ CAUGHT BY test/setup.test.js, NOT BY READING. Three rounds of reading this file missed
  // it; the four failing assertions named it in one run.
  //
  // So: the message lives here, and render() draws it. Same rule as `chosen`.
  let msgText = "";
  let msgKind = "";
  // ⚠️ AND THE SAME PATTERN A THIRD TIME: whether the text-path fallback is OPEN is state.
  //
  // The picker's failure handler did `inp.style.display = ""` and then called render() — which
  // built a BRAND NEW input element with `display: none`, discarding the one that had just been
  // revealed. **The fallback appeared and was removed in the same tick**, which is word for word
  // the reported symptom of the folder-picker bug two rounds ago.
  //
  // ⚠️ THE RULE, and it took three bugs to state it: ANYTHING A USER CAN CHANGE IS MODULE STATE,
  // AND render() DRAWS IT. A direct DOM write in an event handler survives exactly until the
  // next render — and every handler here ends with a render.
  let typedOpen = false;

  const el = (id) => document.getElementById(id);
  const base = (p) => String(p).replace(/\/+$/, "").split("/").pop() || p;

  // ⚠️⚠️ AN ERROR AREA INSIDE THE PANEL, BECAUSE THE BANNER IS UNDERNEATH IT.
  //
  // #setup-overlay is `inset: 0; z-index: 20` and a SIBLING of #box, so it covers the banner
  // completely. Every `setBanner("warn", ...)` in this file was therefore written to a pixel
  // rectangle the user cannot see — including all of start()'s failure messages. **The panel
  // simply closed, or did nothing, and said nothing.**
  //
  // ⚠️ A message about a panel belongs IN the panel. The overlay is a modal surface; anything
  // the user must read while it is open has to be drawn on it.
  function note(msg, kind) {
    msgText = msg || "";
    msgKind = kind || "";
    paintNote();
  }

  // ⚠️ PAINTS FROM STATE, AND IS CALLED BY render(). Splitting "set the message" from "draw the
  // message" is what makes a message survive the re-render that follows it.
  function paintNote() {
    const m = el("setup-msg");
    if (!m) return;
    m.className = "setup-msg" + (msgKind ? " " + msgKind : "");
    m.textContent = msgText;
  }

  async function refresh() {
    const r = await api("/api/roots");
    if (!r.ok) {
      // ⚠️ THIS USED TO `return` AND LEAVE AN EMPTY PANEL. The user would see a title and no
      // folders — no list, no buttons, no reason — which reads as the app being broken rather
      // than a request having failed.
      note(`Could not load your folders: ${r.error || "the search engine is not responding"}`, "err");
      render();
      return;
    }
    roots = { configured: r.configured || [], indexed: r.indexed || [],
              suggested: r.suggested || [] };
    // ⚠️ PRE-SELECT WHAT EXISTS. A first-run user pressing one button is the goal; making them
    // tick three boxes first is a step that buys nothing.
    if (!chosen.size) {
      roots.suggested.filter((s) => s.exists).forEach((s) => chosen.add(s.path));
      roots.configured.forEach((p) => chosen.add(p));
    }

    render();
  }

  function render() {
    const host = el("setup");
    if (!host) return;
    host.textContent = "";

    const h = document.createElement("div");
    h.className = "setup-title";
    h.textContent = "What should be searchable?";
    host.appendChild(h);

    const sub = document.createElement("div");
    sub.className = "setup-sub";
    // ⚠️ THE TIME COST IS STATED UP FRONT. Indexing 5,000 documents took 1h 21m on this machine,
    // and a user who starts that without knowing will kill it and conclude the app froze.
    sub.textContent = "Nothing leaves your machine. Indexing a few thousand files can take an "
                    + "hour or more — you can keep using search while it runs.";
    host.appendChild(sub);

    // ⚠️ THE MESSAGE SLOT, ALWAYS PRESENT AND USUALLY EMPTY. Created here rather than shown and
    // hidden, so render() never has to decide whether the panel has one.
    const msg = document.createElement("div");
    msg.id = "setup-msg";
    msg.className = "setup-msg";
    host.appendChild(msg);
    // ⚠️ DRAWN FROM STATE on every render, so a message set before a render is still there after it.
    paintNote();

    // ⚠️⚠️ ONE LIST, BUILT FROM THE UNION OF EVERY SOURCE.
    //
    // This redrew only from `roots.suggested` and `roots.indexed` — the two things the SERVER
    // knows about — while `chosen` held what the USER had picked. So the picker added folders to
    // `chosen`, appended rows for them, and then called render(), which wiped the panel and
    // redrew from the server's lists. **The selected folders vanished the instant they were
    // chosen**, which is exactly what was reported.
    //
    // ⚠️ THE ERROR WAS TWO SOURCES OF TRUTH FOR ONE LIST. `chosen` is the state; the panel must
    // be drawn FROM that state, not from a different list that happens to overlap with it.
    const list = document.createElement("div");
    list.className = "setup-list" + (wholeDevice ? " disabled" : "");

    const cands = new Map();          // path -> { label, count, known }
    for (const s of roots.suggested) {
      if (s.exists) cands.set(s.path, { label: s.name || base(s.path), count: 0, known: true });
    }
    // ⚠️ Already-indexed roots carry their REAL document counts, because the configured roots and
    // what is actually in the index drift apart as soon as something is renamed or unmounted.
    for (const i of roots.indexed) {
      // ⚠️ NO PLATFORM-SPECIFIC FILTER. This read `if (!i.path.startsWith("/Users/")) continue;`
      // — a hardcoded macOS home prefix. On Windows or Linux every indexed root would have been
      // hidden from this list, so the panel would show nothing configured while the index held
      // thousands of documents. The app is meant to be open source and cross-platform.
      cands.set(i.path, { label: base(i.path), count: i.documents || 0, known: true });
    }
    // ⚠️ AND EVERYTHING THE USER PICKED, which is the half that was missing entirely.
    for (const p of chosen) {
      if (!cands.has(p)) cands.set(p, { label: base(p), count: 0 });
    }

    for (const [path, meta] of cands) {
      list.appendChild(row(path, meta.label, chosen.has(path), meta.count,
                           !meta.known));
    }
    // ⚠️ A COUNT OF WHAT IS ABOUT TO BE INDEXED. "Index these folders" gave no sense of whether
    // that is thirty seconds or three hours, and the only way to find out was to start it.
    const known = [...chosen].map((p) => cands.get(p)).filter((m) => m && m.count);
    const unknown = [...chosen].filter((p) => !(cands.get(p) || {}).count).length;
    if (chosen.size) {
      const parts = [];
      if (known.length) {
        const total = known.reduce((a, m) => a + m.count, 0);
        parts.push(`${total.toLocaleString()} documents already indexed`);
      }
      if (unknown) parts.push(`${unknown} folder${unknown > 1 ? "s" : ""} not indexed yet`);
      const est = document.createElement("div");
      est.className = "setup-est";
      est.textContent = wholeDevice ? "Entire device: every folder will be crawled"
                                    : `Selected: ${parts.join(" \u00b7 ")}`;
      host.appendChild(est);
    }
    // ⚠️ A PICKED FOLDER THAT MATCHED NOTHING IS NOT AN ERROR — it simply has no count yet, and
    // the "0 indexed" state is what tells the user it has not been crawled.
    host.appendChild(list);

    // ⚠️⚠️ A BUTTON THAT OPENS THE MACOS PICKER, NOT A TEXT INPUT.
    //
    // This was an input box reading "or paste a folder path". That asks the user to know where
    // the folder is, spell it exactly, and know whether the system calls it ~/Documents or
    // ~/documents — and when they get it wrong the answer is "not a directory", which is the
    // tool blaming them for not knowing something it could simply have shown them.
    //
    // ⚠️ The text input survives as a fallback, because typing a path is genuinely faster when
    // you already know it, and remote or unusual paths are awkward in a picker.
    const add = document.createElement("div");
    add.className = "setup-add";

    const pick = document.createElement("button");
    pick.className = "pick";
    pick.textContent = "Choose folders…";
    pick.onclick = async () => {
      pick.disabled = true;
      pick.textContent = "Choosing…";
      try {
        const got = await invoke("pick_folder", { multiple: true });
        // ⚠️ ONLY MUTATE STATE; render() OWNS THE DOM. Appending rows here and then re-rendering
        // is what produced the original bug — the panel was built twice, from two sources, and
        // the second build did not know about the first.
        for (const p of got || []) {
          const clean = String(p).replace(/\/+$/, "");
          if (clean) { chosen.add(clean); extras.add(clean); }
        }
        if (!(got || []).length) note("");     // a cancelled dialog is not an error
      } catch (e) {
        // ⚠️ FALL BACK TO THE TEXT BOX RATHER THAN DOING NOTHING, AND SAY WHY. If the native
        // dialog is unavailable the user must still be able to add a folder — and must be told
        // that is what happened rather than left wondering why the button did nothing.
        note(`The folder picker could not open (${e}). Type a path instead.`, "warn");
        typedOpen = true;      // ⚠️ state, so the next render draws it open
      }
      pick.disabled = false;
      pick.textContent = "Choose folders…";
      render();
    };

    const inp = document.createElement("input");
    inp.type = "text";
    inp.style.display = typedOpen ? "" : "none";
    inp.placeholder = "type a folder path…";
    inp.spellcheck = false;
    const btn = document.createElement("button");
    btn.textContent = "Add";
    btn.style.display = typedOpen ? "" : "none";
    const doAdd = () => {
      const v = inp.value.trim();
      if (!v) return;
      // ⚠️ Same rule: state only, then re-render. And the path is added even if it does not
      // exist yet — the server is the thing that validates, and a client-side check that
      // disagrees with it is worse than none.
      const clean = v.replace(/\/+$/, "");
      chosen.add(clean);
      extras.add(clean);
      inp.value = "";
      render();
    };
    btn.onclick = doAdd;
    inp.onkeydown = (e) => { if (e.key === "Enter") doAdd(); };

    const type = document.createElement("button");
    type.textContent = "or type a path";
    type.onclick = () => {
      typedOpen = !typedOpen;
      render();
      if (typedOpen) {
        const i2 = el("setup").querySelector(".setup-add input");
        if (i2) i2.focus();
      }
    };

    add.append(pick, type, inp, btn);
    host.appendChild(add);

    // ⚠️⚠️ "EVERYTHING" WAS IN THE BACKEND AND NOT IN THE INTERFACE.
    // `config.MODES` has had a mode that indexes the whole home directory since the first
    // commit, and the setup screen never offered it — so the only way to reach it was a
    // command-line flag. A capability nobody can find is a capability that does not exist.
    const whole = document.createElement("label");
    whole.className = "setup-row whole";
    const wcb = document.createElement("input");
    wcb.type = "checkbox";
    wcb.checked = wholeDevice;
    const wt = document.createElement("span");
    wt.className = "setup-name";
    wt.textContent = "Entire device";
    const wp = document.createElement("span");
    wp.className = "setup-path";
    // ⚠️ THE COST IS STATED UP FRONT. "Everything" sounds free and is not: it is every
    // indexable file in the home directory, and the first index of it is the slowest run the
    // app will ever do.
    wp.textContent = "every folder in your home directory — the slowest first index";
    whole.append(wcb, wt, wp);
    wcb.onchange = () => {
      wholeDevice = wcb.checked;
      render();
      // ⚠️ THE FOLDERS ARE NOT LOST, BUT THEY ARE NOT USED EITHER, AND THE OLD BEHAVIOUR SAID
      // NOTHING. A user who ticked three folders and then ticked "Entire device" would start a
      // job that ignored all three, with no indication that had happened.
      if (wholeDevice && chosen.size) {
        note(`Entire device is selected, so the ${chosen.size} folder(s) above are ignored. `
             + `Untick it to index only those.`, "warn");
      } else {
        note("");
      }
    };
    host.appendChild(whole);

    const foot = document.createElement("div");
    foot.className = "setup-foot";
    const go = document.createElement("button");
    go.className = "primary";
    go.textContent = busy ? "Starting…" : "Index these folders";
    go.disabled = busy || (!wholeDevice && chosen.size === 0);
    go.onclick = start;
    const skip = document.createElement("button");
    skip.textContent = "Not now";
    skip.onclick = () => close();
    foot.append(go, skip);
    host.appendChild(foot);
  }

  function row(path, label, checked, count, removable) {
    const d = document.createElement("label");
    d.className = "setup-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = checked;
    cb.onchange = () => {
        // ⚠️ RE-RENDER, NOT JUST MUTATE. The Index button's enabled state is derived from
        // `chosen.size`, so without this, ticking a folder when none was ticked left the button
        // DISABLED and unticking every folder left it ENABLED — wrong in both directions.
        if (cb.checked) chosen.add(path); else chosen.delete(path);
        render();
      };
    const t = document.createElement("span");
    t.className = "setup-name";
    t.textContent = label;
    const p = document.createElement("span");
    p.className = "setup-path";
    p.textContent = count ? `${path}  ·  ${count.toLocaleString()} indexed` : path;
    d.append(cb, t, p);
    // ⚠️ A REMOVE AFFORDANCE, BECAUSE UNTICKING IS NOT REMOVING. Untick a folder you picked by
    // mistake and it stays in the list for ever — there is no way back to the list you started
    // with. Only hand-picked rows get the button: the suggested ones are options, and removing
    // an option you have not chosen is not a thing you can want.
    if (removable) {
      const x = document.createElement("button");
      x.className = "setup-x";
      x.textContent = "\u00d7";
      x.title = "remove from this list";
      x.onclick = (e) => {
        e.preventDefault();
        chosen.delete(path);
        extras.delete(path);
        render();
      };
      d.appendChild(x);
    }
    return d;
  }

  async function start() {
    if (busy) return;
    busy = true;
    render();
    note("Starting…");

    // ⚠️ TWO CALLS ON PURPOSE: set the roots (instant, reversible), THEN start indexing (long).
    // Fusing them would make a mistyped path begin an hour-long job.
    const setr = await api("/api/roots",
                           { method: "POST",
                             body: { roots: [...chosen], mode: wholeDevice ? "everything"
                                                                          : "explicit" } });
    if (!setr.ok) {
      busy = false;
      note(`Could not save those folders: ${setr.error}`, "err");
      render();
      return;
    }
    const idx = await api("/api/index", { method: "POST", body: {} });
    busy = false;
    if (!idx.ok) {
      // ⚠️ STAY OPEN AND SHOW THE ERROR. Closing the panel and writing the reason to the banner
      // — which the overlay was covering — left the user with a dialog that vanished and no
      // explanation of why nothing started.
      note(`Could not start indexing: ${idx.error}`, "err");
      render();
      return;
    }
    close();
    setBanner("info", "Indexing… 0%");
    startPolling();
  }

  // ⚠️ THE OVERLAY IS WHAT SHOWS AND HIDES, not the panel inside it. Toggling the inner element
  // would leave the opaque full-window backdrop in place with nothing on it.
  function open() {
    el("setup-overlay").classList.add("show");
    refresh();
  }
  function close() {
    el("setup-overlay").classList.remove("show");
    msgText = ""; msgKind = "";
    // ⚠️ FOCUS RETURNS TO THE SEARCH BOX. Closing a panel and leaving focus on the document means
    // the next keystroke does nothing at all, which reads as the app having frozen.
    const q = el("q"); if (q) q.focus();
  }
  function isOpen() { return el("setup-overlay")?.classList.contains("show"); }

  return { refresh, open, close, isOpen: () => el("setup-overlay")?.classList.contains("show") };
})();
