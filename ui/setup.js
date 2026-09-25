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
    list.className = "setup-list";
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

    const add = document.createElement("div");
    add.className = "setup-add";
    const inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = "or paste a folder path…";
    inp.spellcheck = false;
    const btn = document.createElement("button");
    btn.textContent = "Add";
    const doAdd = () => {
      const v = inp.value.trim().replace(/^~/, window.__HOME__ || "~");
      if (!v) return;
      // ⚠️ The PATH IS NOT VALIDATED HERE — the server rejects a non-directory, and it must,
      // because only the server can resolve `~` correctly and a client-side check that
      // disagrees with the server is worse than none.
      chosen.add(v);
      inp.value = "";
      list.appendChild(row(v, v.split("/").pop() || v, false));
    };
    btn.onclick = doAdd;
    inp.onkeydown = (e) => { if (e.key === "Enter") doAdd(); };
    add.append(inp, btn);
    host.appendChild(add);

    const foot = document.createElement("div");
    foot.className = "setup-foot";
    const go = document.createElement("button");
    go.className = "primary";
    go.textContent = busy ? "Starting…" : "Index these folders";
    go.disabled = busy || chosen.size === 0;
    go.onclick = start;
    const skip = document.createElement("button");
    skip.textContent = "Not now";
    skip.onclick = () => el("setup").classList.remove("show");
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
    const setr = await api("/api/roots", { method: "POST", body: { roots: [...chosen] } });
    if (!setr.ok) {
      busy = false;
      setBanner("warn", `Could not set folders: ${setr.error}`);
      render();
      return;
    }
    const idx = await api("/api/index", { method: "POST", body: {} });
    busy = false;
    el("setup").classList.remove("show");
    if (!idx.ok) setBanner("warn", `Could not start indexing: ${idx.error}`);
    else { setBanner("info", "Indexing… 0%"); startPolling(); }
    render();
  }

  function open() { refresh().then(() => el("setup").classList.add("show")); }
  function close() { el("setup").classList.remove("show"); }

  return { refresh, open, close, isOpen: () => el("setup")?.classList.contains("show") };
})();
