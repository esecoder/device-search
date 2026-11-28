// setup.js — the first-run screen, so the app is usable without a terminal.
//
// WHY THIS EXISTS AND WHY IT IS NOT A NICETY.
// The app used to do exactly nothing on first launch against an empty index: no message, no
// button, no hint that a terminal command was required. **"Launch it and run `ds index`" is a
// developer workflow shipped to a user**, and the user's reaction — "so what am I supposed to
// do?" — is the correct one.
//
// THE STATE MACHINE, and every state is reachable:
//
// no roots, no documents -> SETUP (pick folders, press Index)
// indexing in progress -> PROGRESS (bar, ETA, search still works on what exists)
// documents, not indexing -> SEARCH (the normal state)
// empty query -> SEARCH (shows what is indexed)
//
// SEARCH IS NEVER DISABLED. During indexing the results are partial, and saying so is
// better than hiding the box — a user who can search 40% of their disk is better served than
// one watching a progress bar with no way to do anything.

const Setup = (() => {
  let roots = { configured: [], indexed: [], suggested: [] };
  let chosen = new Set();
  // PATHS THE USER ADDED BY HAND, kept SEPARATE FROM THE SERVER'S SUGGESTIONS. render() needs
  // to know which rows are "options you were offered" and which are "things you added", because
  // only the second can be removed.
  let extras = new Set();
  let busy = false;
  // THE WHOLE-DEVICE CHOICE, CARRIED THROUGH TO THE API. `POST /api/roots` takes a mode,
  // and the backend has always understood "everything" — this is only the interface catching up.
  let wholeDevice = false;
  // TWO STEPS IN ONE PANEL, WHICH IS THE THING THAT WAS ASKED FOR AND THE THING THAT WAS
  // WRONG TWICE BEFORE.
  //
  // 1. it was in the folder list — a privacy decision styled like a checkbox next to
  // Documents, which teaches the user to click past it
  // 2. it was its own panel — correct separation, but a second place to look, a second
  // shortcut to remember, and a connection that setup never
  // mentioned
  //
  // A STEP GETS BOTH. The two decisions stay visually distinct — different screens, different
  // heading, different words — while living in one panel the user already knows how to open.
  let step = "folders";
  // THE MESSAGE IS STATE, NOT A DOM WRITE, AND THIS IS THE THIRD TIME TODAY.
  //
  // note() wrote straight into #setup-msg. But render() begins with `host.textContent = ""`,
  // so every sequence of
  //
  // note("something failed"); render();
  //
  // printed the message and then DELETED IT. Which is exactly the bug that made picked folders
  // vanish — render() wiping DOM that held state — and I fixed that one without looking for
  // the same shape two functions away.
  //
  // CAUGHT BY test/setup.test.js, NOT BY READING. Three rounds of reading this file missed
  // it; the four failing assertions named it in one run.
  //
  // So: the message lives here, and render() draws it. Same rule as `chosen`.
  let msgText = "";
  let msgKind = "";

  // Folder sizes, fetched once. See the note in the folders step.
  const sizesCache = new Map();
  let sizesAsked = false;

  // Folder tree state: which parent is being shown, and which folders are ticked.
  // Kept at module level so a re-render (switching tabs) does not lose the
  // selection the user just made.
  // AND THE SAME PATTERN A THIRD TIME: whether the text-path fallback is OPEN is state.
  //
  // The picker's failure handler did `inp.style.display = ""` and then called render() — which
  // built a BRAND NEW input element with `display: none`, discarding the one that had just been
  // revealed. **The fallback appeared and was removed in the same tick**, which is word for word
  // the reported symptom of the folder-picker bug two rounds ago.
  //
  // THE RULE, and it took three bugs to state it: ANYTHING A USER CAN CHANGE IS MODULE STATE,
  // AND render() DRAWS IT. A direct DOM write in an event handler survives exactly until the
  // next render — and every handler here ends with a render.
  let typedOpen = false;

  const el = (id) => document.getElementById(id);
  const base = (p) => String(p).replace(/\/+$/, "").split("/").pop() || p;

  // AN ERROR AREA INSIDE THE PANEL, BECAUSE THE BANNER IS UNDERNEATH IT.
  //
  // #setup-overlay is `inset: 0; z-index: 20` and a SIBLING of #box, so it covers the banner
  // completely. Every `setBanner("warn", ...)` in this file was therefore written to a pixel
  // rectangle the user cannot see — including all of start()'s failure messages. **The panel
  // simply closed, or did nothing, and said nothing.**
  //
  // A message about a panel belongs IN the panel. The overlay is a modal surface; anything
  // the user must read while it is open has to be drawn on it.
  function note(msg, kind) {
    msgText = msg || "";
    msgKind = kind || "";
    paintNote();
  }

  // PAINTS FROM STATE, AND IS CALLED BY render(). Splitting "set the message" from "draw the
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
      // THIS USED TO `return` AND LEAVE AN EMPTY PANEL. The user would see a title and no
      // folders — no list, no buttons, no reason — which reads as the app being broken rather
      // than a request having failed.
      note(`Could not load your folders: ${r.error || "the search engine is not responding"}`, "err");
      render();
      return;
    }
    roots = { configured: r.configured || [], indexed: r.indexed || [],
              suggested: r.suggested || [] };
    // PRE-SELECT WHAT EXISTS. A first-run user pressing one button is the goal; making them
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

    // THE STEP HEADER. Two labels, the active one marked, and the AI step labelled
    // "optional" so nobody thinks it is a requirement to finish setup.
    const tabs = document.createElement("div");
    tabs.className = "setup-tabs";
    for (const [id, label] of [["folders", "Folders"], ["ai", "AI (optional)"],
                               ["code", "Code search"], ["media", "Media"],
                               ["storage", "Storage"]]) {
      const t = document.createElement("button");
      t.className = "setup-tab" + (step === id ? " on" : "");
      t.textContent = label;
      t.onclick = () => { step = id; render(); };
      tabs.appendChild(t);
    }
    host.appendChild(tabs);

    const h = document.createElement("div");
    h.className = "setup-title";
    h.textContent = step === "ai" ? "Connect a model to ask questions"
                 : step === "code" ? "Faster code-fragment search"
                 : step === "media" ? "Pictures, video and audio"
                 : step === "storage" ? "What is using disk space"
                                   : "What should be searchable?";
    host.appendChild(h);

    // THE MODEL STEP IS ITS OWN RENDERER, DRAWN INTO THIS PANEL. Splitting it this way keeps
    // one place that decides what is on screen (this function) and lets the AI step own its own
    // form without the folder code knowing anything about it.
    if (step === "ai") {
      const box = document.createElement("div");
      box.id = "ai-step";
      host.appendChild(box);
      AI.renderInto(box);
      return;
    }


  // ---- storage -------------------------------------------------------------
  //
  // Every part of the index, what it costs on disk, what it buys, and a button to
  // remove it. The consequence is stated on the row BEFORE the button is pressed,
  // and the confirmation repeats it in a modal - deleting 7 GB must never be one
  // click away from a misread label.
  if (step === "storage") {
    const host2 = document.createElement("div");
    host2.className = "storage";
    host2.textContent = "Reading\u2026";
    host.appendChild(host2);

    const gb = (n) => n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB`
                 : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB`
                 : `${(n / 1e3).toFixed(0)} KB`;

    (async () => {
      const r = await api("/api/storage");
      if (!r.ok) { host2.textContent = r.error || "Could not read disk usage."; return; }
      host2.textContent = "";

      const total = document.createElement("div");
      total.className = "storage-total";
      total.textContent = `${gb(r.total)} in ${r.directory}  \u00b7  ${gb(r.free)} free on this disk`;
      host2.appendChild(total);

      // UNINSTALL, AT THE BOTTOM WHERE IT CANNOT BE PRESSED BY ACCIDENT.
      // Dragging the app to the Trash removes the program and leaves all of this
      // behind. A user who wants their disk back needs a way to say so.
      (async () => {
        const info = await api("/api/uninstall-info");
        if (!info.ok) return;
        const foot = document.createElement("div");
        foot.className = "storage-uninstall";
        const text = document.createElement("div");
        text.className = "storage-what";
        text.textContent =
          `Uninstalling the app does not remove the ${gb(info.bytes)} above. `
          + "Delete it here if you want the disk space back.";
        foot.appendChild(text);
        const btn = document.createElement("button");
        btn.className = "setup-btn danger";
        btn.textContent = `Remove everything · frees ${gb(info.bytes)}`;
        btn.onclick = () => confirmUninstall(info, btn);
        foot.appendChild(btn);
        host2.appendChild(foot);
      })();

      for (const item of (r.items || [])) {
        if (!item.present && !item.bytes) continue;
        const row = document.createElement("div");
        row.className = "storage-row";

        const head = document.createElement("div");
        head.className = "storage-head";
        const name = document.createElement("span");
        name.className = "storage-name";
        name.textContent = item.label;
        const size = document.createElement("span");
        size.className = "storage-size";
        size.textContent = gb(item.bytes);
        head.appendChild(name);
        head.appendChild(size);
        row.appendChild(head);

        const what = document.createElement("div");
        what.className = "storage-what";
        what.textContent = item.what;
        row.appendChild(what);

        const cost = document.createElement("div");
        cost.className = "storage-cost";
        cost.textContent = `Without it: ${item.cost}`;
        row.appendChild(cost);

        if (item.deletable && item.bytes > 0) {
          const del = document.createElement("button");
          del.className = "setup-btn danger";
          del.textContent = `Delete \u00b7 frees ${gb(item.bytes)}`;
          del.onclick = () => confirmDelete(item, del, host2);
          row.appendChild(del);
        }
        host2.appendChild(row);
      }
    })();
    return;
  }

  // ---- folder tree with live cost ------------------------------------------
  //
  // Every folder shows what it would cost before anything is committed: how many
  // files, how much index, how long. The totals update as boxes are ticked, so the
  // consequence of a choice is visible while making it rather than after.
  //
  // The numbers come from the backend, computed from what is already indexed. A
  // disk walk would take minutes on a home directory and make the checkboxes feel
  // broken.

  // Pictures, video and audio. Each is a separate index with its own cost,
  // because they fail in different ways and take wildly different times:
  // images are seconds, transcription is minutes.
  if (step === "media") {
    const box = document.createElement("div");
    box.className = "media-step";
    box.innerHTML = "<p>Find a picture or a video by describing it, and search "
                  + "what was said in a recording.</p>"
                  + "<p class='setup-sub'>Optional. Text search works without any of it. "
                  + "These take a few minutes and run in the background.</p>"
                  + "<div id='media-cost' class='setup-sub'>Checking the cost…</div>"
                  + "<div id='media-actions'></div>";
    host.appendChild(box);

    const costEl = box.querySelector("#media-cost");
    const actEl = box.querySelector("#media-actions");

    (async () => {
      const r = await api("/api/media");
      if (!r.ok) { costEl.textContent = r.error || "Could not read the cost."; return; }
      const d = r;
      const lines2 = [];
      lines2.push(d.image_index_built
        ? `Pictures: ${d.images.toLocaleString()} indexed.`
        : `Pictures: ${d.images.toLocaleString()} found, about ${d.image_minutes} min to index.`);
      lines2.push(d.video_index_built
        ? `Video: ${d.video.toLocaleString()} files, ${d.frames.toLocaleString()} frames indexed.`
        : `Video and audio: ${d.video.toLocaleString()} video, ${d.audio.toLocaleString()} audio, `
          + `about ${d.media_minutes} min for ${d.frames.toLocaleString()} frames.`);
      costEl.innerHTML = lines2.join("<br>");

      const mk = (label, kind, enabled) => {
        const b = document.createElement("button");
        b.className = "setup-btn";
        b.textContent = label;
        b.disabled = !enabled;
        if (!enabled) b.title = "already built";
        b.onclick = async () => {
          b.disabled = true;
          b.textContent = "Starting…";
          const s2 = await api("/api/build-media", { method: "POST", body: { kind } });
          if (!s2.ok) { b.disabled = false; b.textContent = label; return; }
          b.textContent = "Running in the background";
        };
        actEl.appendChild(b);
      };

      // ONE ACTION, TWO KINDS OF MEDIA, AND SAY SO.
      //
      // The button read "Transcribe and index video", so a user with 44 audio
      // files had no way to know their recordings were covered by it. Audio is
      // transcribed by the same run; the label now says both.
      //
      // And when a button is disabled because the work is already done, the reason
      // is on screen rather than in a tooltip. "Index pictures" greyed out with no
      // explanation reads as broken, not as finished.
      if (d.image_index_built) {
        const done = document.createElement("div");
        done.className = "setup-sub";
        done.textContent = "Pictures are already indexed — search finds them by description.";
        actEl.appendChild(done);
      } else {
        mk(`Index ${d.images.toLocaleString()} pictures`, "images", true);
      }

      if (d.video_index_built) {
        const done = document.createElement("div");
        done.className = "setup-sub";
        done.textContent = "Video frames are already indexed.";
        actEl.appendChild(done);
      } else {
        mk(`Transcribe ${d.audio.toLocaleString()} audio files and `
           + `${d.video.toLocaleString()} videos`, "media", true);
      }

      // WHILE A BUILD IS RUNNING, SAY HOW FAR ALONG IT IS.
      //
      // The button used to change to "Running in the background" and then nothing
      // happened on screen for the next forty-six minutes. The status is already
      // written by the build; this reads it back and updates the line in place.
      if (d.building && d.building.running) {
        const prog = document.createElement("div");
        prog.className = "setup-sub";
        actEl.appendChild(prog);
        const tick = async () => {
          const rr = await api("/api/media");
          if (!rr.ok) return;
          const b = (rr.building) || {};
          const st = (rr.status) || {};
          if (b.running && st.documents_total) {
            const done = st.documents_done || 0;
            const total = st.documents_total || 1;
            const mins = Math.round((st.eta_seconds || 0) / 60);
            prog.textContent = `Indexing ${done.toLocaleString()} of `
                             + `${total.toLocaleString()} (${Math.round(done / total * 100)}%)`
                             + (mins > 0 ? ` — about ${mins} min left` : "");
            setTimeout(tick, 3000);
          } else {
            prog.textContent = "Finished. Reopen this tab to see the new counts.";
          }
        };
        setTimeout(tick, 1500);
      }
    })();
    return;
  }

  // The code-fragment index is the one optional piece that can use more disk than the
    // documents it indexes, so it is never started without the cost being shown and the
    // choice being clicked. The numbers come from the backend, computed from the corpus,
    // rather than being written here where they would go stale.
    if (step === "code") {
      const box = document.createElement("div");
      box.className = "code-step";
      // Plain language. The previous copy explained "an index of every
      // three-character sequence", which is accurate and means nothing to the
      // person deciding whether to click.
      box.innerHTML = "<p>Makes searching for a piece of code, like "
                    + "<code>InputLayer(shape=(784,))</code>, almost instant.</p>"
                    + "<p class='setup-sub'>Without it those searches still work, "
                    + "they just scan everything and take a few seconds.</p>"
                    + "<div id='code-cost' class='setup-sub'>Checking the cost…</div>"
                    + "<div id='code-actions'></div>";
      host.appendChild(box);

      const costEl = box.querySelector("#code-cost");
      const actEl = box.querySelector("#code-actions");

      (async () => {
        const r = await api("/api/deep-index");
        if (!r.ok) { costEl.textContent = r.error || "Could not read the cost."; return; }
        const d = r || {};
        if (d.built) {
          costEl.textContent = "The code-fragment index is already built.";
          return;
        }
        if (d.estimated_gb > d.free_gb) {
          costEl.textContent = `Needs about ${d.estimated_gb} GB but only ${d.free_gb} GB `
                             + "is free. Free up space first.";
          return;
        }
        costEl.textContent = `${d.documents.toLocaleString()} documents · about `
                           + `${d.estimated_gb} GB · about ${d.estimated_minutes} minutes. `
                           + `You have ${d.free_gb} GB free.`;

        const go = document.createElement("button");
        go.className = "setup-btn";
        go.textContent = "Build it";
        go.onclick = async () => {
          go.disabled = true;
          go.textContent = "Starting…";
          const s2 = await api("/api/deep-index", { method: "POST", body: {} });
          if (!s2.ok) { go.disabled = false; go.textContent = "Build it"; return; }
          costEl.textContent = "Building in the background. Search keeps working; "
                             + "progress appears in the banner.";
          go.remove();
        };
        actEl.appendChild(go);
      })();
      return;
    }

    const sub = document.createElement("div");
    sub.className = "setup-sub";
    // THE TIME COST IS STATED UP FRONT. Indexing 5,000 documents took 1h 21m on this machine,
    // and a user who starts that without knowing will kill it and conclude the app froze.
    sub.textContent = "Nothing leaves your machine. Indexing a few thousand files can take an "
                    + "hour or more — you can keep using search while it runs.";
    host.appendChild(sub);

    // THE MESSAGE SLOT, ALWAYS PRESENT AND USUALLY EMPTY. Created here rather than shown and
    // hidden, so render() never has to decide whether the panel has one.
    const msg = document.createElement("div");
    msg.id = "setup-msg";
    msg.className = "setup-msg";
    host.appendChild(msg);

    // There used to be a SECOND folder list here: a tree of subfolders with their
    // own costs, alongside the list below. Two lists, two independent selections,
    // and the tree only rendered while its state was empty - so it appeared on the
    // first open and was missing on every visit after.
    //
    // The list below is the one that works: it owns the checkbox state, the remove
    // button and the whole-device option. The cost information moves onto those
    // rows instead.
    // DRAWN FROM STATE on every render, so a message set before a render is still there after it.
    paintNote();

    // ONE LIST, BUILT FROM THE UNION OF EVERY SOURCE.
    //
    // This redrew only from `roots.suggested` and `roots.indexed` — the two things the SERVER
    // knows about — while `chosen` held what the USER had picked. So the picker added folders to
    // `chosen`, appended rows for them, and then called render(), which wiped the panel and
    // redrew from the server's lists. **The selected folders vanished the instant they were
    // chosen**, which is exactly what was reported.
    //
    // THE ERROR WAS TWO SOURCES OF TRUTH FOR ONE LIST. `chosen` is the state; the panel must
    // be drawn FROM that state, not from a different list that happens to overlap with it.
    const list = document.createElement("div");
    list.className = "setup-list" + (wholeDevice ? " disabled" : "");

    // Sizes for the rows below, fetched once and cached at module scope.
    //
    // This render function is NOT async, so the request cannot be awaited here.
    // It starts in the background and re-renders when it lands, guarded by
    // `sizesAsked` so the re-render cannot start another one - which would be an
    // infinite loop rather than a slow panel.
    const sizes = sizesCache;
    if (!sizesAsked) {
      sizesAsked = true;
      api("/api/folders").then((fr) => {
        for (const f of (fr && fr.folders) || []) {
          sizesCache.set(f.path, f.index_bytes || 0);
        }
        if (sizesCache.size) render();
      }).catch(() => { /* the list still works without sizes */ });
    }

    const cands = new Map(); // path -> { label, count, known }
    for (const s of roots.suggested) {
      if (s.exists) cands.set(s.path, { label: s.name || base(s.path), count: 0, known: true });
    }
    // Already-indexed roots carry their REAL document counts, because the configured roots and
    // what is actually in the index drift apart as soon as something is renamed or unmounted.
    for (const i of roots.indexed) {
      // NO PLATFORM-SPECIFIC FILTER. This read `if (!i.path.startsWith("/Users/")) continue;`
      // — a hardcoded macOS home prefix. On Windows or Linux every indexed root would have been
      // hidden from this list, so the panel would show nothing configured while the index held
      // thousands of documents. The app is meant to be open source and cross-platform.
      cands.set(i.path, { label: base(i.path), count: i.documents || 0, known: true });
    }
    // AND EVERYTHING THE USER PICKED, which is the half that was missing entirely.
    for (const p of chosen) {
      if (!cands.has(p)) cands.set(p, { label: base(p), count: 0 });
    }

    for (const [path, meta] of cands) {
      list.appendChild(row(path, meta.label, chosen.has(path), meta.count,
                           !meta.known, sizes.get(path) || 0));
    }
    // A COUNT OF WHAT IS ABOUT TO BE INDEXED. "Index these folders" gave no sense of whether
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
    // A PICKED FOLDER THAT MATCHED NOTHING IS NOT AN ERROR — it simply has no count yet, and
    // the "0 indexed" state is what tells the user it has not been crawled.
    host.appendChild(list);

    // A BUTTON THAT OPENS THE MACOS PICKER, NOT A TEXT INPUT.
    //
    // This was an input box reading "or paste a folder path". That asks the user to know where
    // the folder is, spell it exactly, and know whether the system calls it ~/Documents or
    // ~/documents — and when they get it wrong the answer is "not a directory", which is the
    // tool blaming them for not knowing something it could simply have shown them.
    //
    // The text input survives as a fallback, because typing a path is genuinely faster when
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
        // ONLY MUTATE STATE; render() OWNS THE DOM. Appending rows here and then re-rendering
        // is what produced the original bug — the panel was built twice, from two sources, and
        // the second build did not know about the first.
        for (const p of got || []) {
          const clean = String(p).replace(/\/+$/, "");
          if (clean) { chosen.add(clean); extras.add(clean); }
        }
        if (!(got || []).length) note(""); // a cancelled dialog is not an error
      } catch (e) {
        // FALL BACK TO THE TEXT BOX RATHER THAN DOING NOTHING, AND SAY WHY. If the native
        // dialog is unavailable the user must still be able to add a folder — and must be told
        // that is what happened rather than left wondering why the button did nothing.
        note(`The folder picker could not open (${e}). Type a path instead.`, "warn");
        typedOpen = true; // state, so the next render draws it open
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
      // Same rule: state only, then re-render. And the path is added even if it does not
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

    // "EVERYTHING" WAS IN THE BACKEND AND NOT IN THE INTERFACE.
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
    // THE COST IS STATED UP FRONT. "Everything" sounds free and is not: it is every
    // indexable file in the home directory, and the first index of it is the slowest run the
    // app will ever do.
    wp.textContent = "every folder in your home directory — the slowest first index";
    whole.append(wcb, wt, wp);
    wcb.onchange = () => {
      wholeDevice = wcb.checked;
      render();
      // THE FOLDERS ARE NOT LOST, BUT THEY ARE NOT USED EITHER, AND THE OLD BEHAVIOUR SAID
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
    // AND THE NEXT STEP IS OFFERED HERE rather than only in a footer link, because the
    // moment a user has chosen folders is the moment they are most likely to accept or decline
    // connecting a model. Asking later means asking someone who has moved on.
    const next = document.createElement("button");
    next.textContent = "Next: connect AI →";
    next.onclick = () => { step = "ai"; render(); };
    go.disabled = busy || (!wholeDevice && chosen.size === 0);
    go.onclick = start;
    const skip = document.createElement("button");
    skip.textContent = "Not now";
    skip.onclick = () => close();
    foot.append(go, next, skip);
    host.appendChild(foot);
  }

  function row(path, label, checked, count, removable, sizeBytes) {
    const d = document.createElement("label");
    d.className = "setup-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = checked;
    cb.onchange = () => {
        // RE-RENDER, NOT JUST MUTATE. The Index button's enabled state is derived from
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
    // The line carries the path and whatever is known about what it costs. The old
    // tree showed a size; it is here now that there is one list instead of two.
    const bits = [path];
    if (count) bits.push(`${count.toLocaleString()} indexed`);
    if (sizeBytes) {
      const mb = sizeBytes / 1e6;
      bits.push(mb >= 1000 ? `${(mb / 1000).toFixed(1)} GB` : `${Math.round(mb)} MB`);
    }
    p.textContent = bits.join(" · ");
    d.append(cb, t, p);
    // A REMOVE AFFORDANCE, BECAUSE UNTICKING IS NOT REMOVING. Untick a folder you picked by
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

    // TWO CALLS ON PURPOSE: set the roots (instant, reversible), THEN start indexing (long).
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
      // STAY OPEN AND SHOW THE ERROR. Closing the panel and writing the reason to the banner
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

  // THE OVERLAY IS WHAT SHOWS AND HIDES, not the panel inside it. Toggling the inner element
  // would leave the opaque full-window backdrop in place with nothing on it.
  // OPEN AT A PANEL, not just at the top.
  //
  // A message that says "index the pictures" and then drops the user on a folder
  // list has told them what to do and made them find it themselves. The panel
  // argument is what makes the button an answer rather than an instruction.
  function open(panel) {
    if (panel && ["folders", "ai", "code", "media"].includes(panel)) {
      step = panel;
      render();
    }
    el("setup-overlay").classList.add("show");
    refresh();
  }
  function close() {
    el("setup-overlay").classList.remove("show");
    msgText = ""; msgKind = "";
    // FOCUS RETURNS TO THE SEARCH BOX. Closing a panel and leaving focus on the document means
    // the next keystroke does nothing at all, which reads as the app having frozen.
    const q = el("q"); if (q) q.focus();
  }
  // A CONFIRMATION THAT STATES THE COST, NOT "ARE YOU SURE?".
  //
  // "Are you sure" asks the user to remember what the previous screen said. This
  // repeats the consequence and names the size being freed, so the decision is made
  // with the facts on screen rather than with a memory of them.
  function confirmDelete(item, button, host) {
    const gb = (n) => n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB`
                 : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${(n / 1e3).toFixed(0)} KB`;
    const back = document.createElement("div");
    back.className = "confirm-back";
    const box = document.createElement("div");
    box.className = "confirm";

    const t = document.createElement("div");
    t.className = "confirm-title";
    t.textContent = `Delete ${item.label}?`;
    box.appendChild(t);

    const size = document.createElement("div");
    size.className = "confirm-size";
    size.textContent = `This frees ${gb(item.bytes)}.`;
    box.appendChild(size);

    const cost = document.createElement("div");
    cost.className = "confirm-cost";
    cost.textContent = item.cost;
    box.appendChild(cost);

    const row = document.createElement("div");
    row.className = "confirm-actions";

    const cancel = document.createElement("button");
    cancel.className = "setup-btn";
    cancel.textContent = "Keep it";
    cancel.onclick = () => back.remove();
    row.appendChild(cancel);

    const go = document.createElement("button");
    go.className = "setup-btn danger";
    go.textContent = "Delete";
    go.onclick = async () => {
      go.disabled = true; cancel.disabled = true;
      go.textContent = "Deleting\u2026";
      const r = await api("/api/storage/delete", { method: "POST", body: { key: item.key } });
      back.remove();
      if (!r.ok) { note(r.error || "could not delete", "err"); return; }
      note(`freed ${gb(r.freed || item.bytes)}`, "ok");
      render();          // redraw with the new sizes
    };
    row.appendChild(go);
    box.appendChild(row);

    back.appendChild(box);
    back.onclick = (e) => { if (e.target === back) back.remove(); };
    document.body.appendChild(back);
  }

  function confirmUninstall(info, button) {
    const gb = (n) => n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB`
                 : n >= 1e6 ? `${(n / 1e6).toFixed(0)} MB` : `${(n / 1e3).toFixed(0)} KB`;
    const back = document.createElement("div");
    back.className = "confirm-back";
    const box = document.createElement("div");
    box.className = "confirm";

    const t = document.createElement("div");
    t.className = "confirm-title";
    t.textContent = "Remove every file this app created?";
    box.appendChild(t);

    const size = document.createElement("div");
    size.className = "confirm-size";
    size.textContent = `This frees ${gb(info.bytes)} and deletes ${info.directory}.`;
    box.appendChild(size);

    const cost = document.createElement("div");
    cost.className = "confirm-cost";
    cost.textContent =
      "Your files are not touched - only the index this app built from them. "
      + "Nothing will be searchable until it is set up and indexed again. "
      + "The app itself stays in Applications; drag it to the Trash afterwards.";
    box.appendChild(cost);

    const row = document.createElement("div");
    row.className = "confirm-actions";
    const cancel = document.createElement("button");
    cancel.className = "setup-btn";
    cancel.textContent = "Keep my index";
    cancel.onclick = () => back.remove();
    row.appendChild(cancel);

    const go = document.createElement("button");
    go.className = "setup-btn danger";
    go.textContent = "Remove everything";
    go.onclick = async () => {
      go.disabled = true; cancel.disabled = true;
      go.textContent = "Removing\u2026";
      const r = await api("/api/uninstall", { method: "POST", body: {} });
      if (!r.ok) {
        back.remove();
        note(r.error || "could not remove", "err");
        return;
      }
      back.remove();
      document.body.innerHTML =
        '<div style="display:flex;height:100%;align-items:center;justify-content:center;'
        + 'font:13px -apple-system;color:#8e8e93;text-align:center;padding:24px">'
        + 'Everything has been removed.<br>The app can now be moved to the Trash.</div>';
    };
    row.appendChild(go);
    box.appendChild(row);
    back.appendChild(box);
    back.onclick = (e) => { if (e.target === back) back.remove(); };
    document.body.appendChild(back);
  }

  function isOpen() { return el("setup-overlay")?.classList.contains("show"); }

  // CLICKING OUTSIDE CLOSES IT.
  //
  // The overlay covers the whole window, so a click anywhere that is not the panel
  // means "I am done here". Without this the only way out was Escape or the
  // Settings link again, which is a trap for anyone who reaches for the mouse.
  //
  // The listener is on the OVERLAY and checks the target, because the panel is its
  // CHILD - a handler on the document would also fire for clicks inside the panel
  // and close it while the user is typing.
  document.addEventListener("mousedown", (e) => {
    const ov = el("setup-overlay");
    if (!ov || !ov.classList.contains("show")) return;
    if (e.target === ov) close();
  });

  return { refresh, open, close, isOpen: () => el("setup-overlay")?.classList.contains("show") };
})();
