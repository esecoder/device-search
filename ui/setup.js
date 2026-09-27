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
  let busy = false;
  // ⚠️ THE WHOLE-DEVICE CHOICE, CARRIED THROUGH TO THE API. `POST /api/roots` takes a mode,
  // and the backend has always understood "everything" — this is only the interface catching up.
  let wholeDevice = false;

  const el = (id) => document.getElementById(id);

  async function refresh() {
    const r = await api("/api/roots");
    if (!r.ok) return;
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

    const list = document.createElement("div");
    list.className = "setup-list" + (wholeDevice ? " disabled" : "");
    const seen = new Set();
    for (const s of roots.suggested) {
      if (!s.exists || seen.has(s.path)) continue;
      seen.add(s.path);
      list.appendChild(row(s.path, s.name, true));
    }
    // ⚠️ Already-indexed roots are shown with their REAL document counts, because the configured
    // roots and what is actually in the index drift apart as soon as something is renamed,
    // unmounted or deleted.
    for (const i of roots.indexed) {
      if (seen.has(i.path) || !i.path.startsWith("/Users/")) continue;
      seen.add(i.path);
      list.appendChild(row(i.path, i.path.split("/").pop() || i.path, true, i.documents));
    }
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
        for (const p of got || []) {
          if (!chosen.has(p)) {
            chosen.add(p);
            list.appendChild(row(p, p.split("/").pop() || p, true));
          }
        }
      } catch (e) {
        // ⚠️ FALL BACK TO THE TEXT BOX RATHER THAN DOING NOTHING. If the native dialog is
        // unavailable the user must still be able to add a folder.
        inp.style.display = "";
        inp.placeholder = `picker unavailable (${e}) — type a path`;
      }
      pick.disabled = false;
      pick.textContent = "Choose folders…";
      render();
    };

    const inp = document.createElement("input");
    inp.type = "text";
    inp.style.display = "none";
    inp.placeholder = "type a folder path…";
    inp.spellcheck = false;
    const btn = document.createElement("button");
    btn.textContent = "Add";
    btn.style.display = "none";
    const doAdd = () => {
      const v = inp.value.trim();
      if (!v) return;
      chosen.add(v);
      inp.value = "";
      list.appendChild(row(v, v.split("/").pop() || v, false));
      render();
    };
    btn.onclick = doAdd;
    inp.onkeydown = (e) => { if (e.key === "Enter") doAdd(); };

    const type = document.createElement("button");
    type.textContent = "or type a path";
    type.onclick = () => {
      const showing = inp.style.display !== "none";
      inp.style.display = showing ? "none" : "";
      btn.style.display = showing ? "none" : "";
      if (!showing) inp.focus();
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
    wcb.onchange = () => { wholeDevice = wcb.checked; render(); };
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
    skip.onclick = () => el("setup-overlay").classList.remove("show");
    foot.append(go, skip);
    host.appendChild(foot);
  }

  function row(path, label, checked, count) {
    const d = document.createElement("label");
    d.className = "setup-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = checked;
    cb.onchange = () => cb.checked ? chosen.add(path) : chosen.delete(path);
    const t = document.createElement("span");
    t.className = "setup-name";
    t.textContent = label;
    const p = document.createElement("span");
    p.className = "setup-path";
    p.textContent = count ? `${path}  ·  ${count.toLocaleString()} indexed` : path;
    d.append(cb, t, p);
    return d;
  }

  async function start() {
    if (busy) return;
    busy = true;
    render();
    setBanner("info", "Starting…");

    // ⚠️ TWO CALLS ON PURPOSE: set the roots (instant, reversible), THEN start indexing (long).
    // Fusing them would make a mistyped path begin an hour-long job.
    const setr = await api("/api/roots",
                           { method: "POST",
                             body: { roots: [...chosen], mode: wholeDevice ? "everything"
                                                                          : "explicit" } });
    if (!setr.ok) {
      busy = false;
      setBanner("warn", `Could not set folders: ${setr.error}`);
      render();
      return;
    }
    const idx = await api("/api/index", { method: "POST", body: {} });
    busy = false;
    el("setup-overlay").classList.remove("show");
    if (!idx.ok) setBanner("warn", `Could not start indexing: ${idx.error}`);
    else { setBanner("info", "Indexing… 0%"); startPolling(); }
    render();
  }

  // ⚠️ THE OVERLAY IS WHAT SHOWS AND HIDES, not the panel inside it. Toggling the inner element
  // would leave the opaque full-window backdrop in place with nothing on it.
  function open() {
    el("setup-overlay").classList.add("show");
    refresh();
  }
  function close() { el("setup-overlay").classList.remove("show"); }
  function isOpen() { return el("setup-overlay")?.classList.contains("show"); }

  return { refresh, open, close, isOpen: () => el("setup")?.classList.contains("show") };
})();
