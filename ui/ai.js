/**
 * ai.js — connecting a model, which is a DIFFERENT DECISION from what to index.
 *
 * ============================================================================
 * ⚠️⚠️ WHY THIS IS NOT IN THE "WHAT SHOULD BE SEARCHABLE?" PANEL
 * ============================================================================
 * The first version put the API key, the endpoint and the model name inside the folder
 * chooser. Two reasons that was wrong, and the second is the serious one.
 *
 * ⚠️ 1. THEY ANSWER DIFFERENT QUESTIONS.
 *       "What should be searchable?" is about what is on the disk.
 *       "Which model?" is about what LEAVES the machine.
 *    Putting them on one screen teaches the user that connecting a model is part of setting
 *    up their folders — a step to click past on the way to something else.
 *
 * ⚠️ 2. IT NORMALISES A PRIVACY DECISION BY HIDING IT IN A ROUTINE ONE.
 *    This tool's premise is that files stay on the machine. Uploading snippets to an API is
 *    the one thing that breaks that premise, and it was sitting in the middle of a list of
 *    folders, styled identically. **A decision that changes where your data goes should not
 *    look like a checkbox next to Documents.**
 *
 * ============================================================================
 * ⚠️⚠️ AND WHY IT ASKS FOR ONE THING INSTEAD OF THREE
 * ============================================================================
 * The old form wanted a key, an endpoint AND a model name. That is a form written by someone
 * who already knows the answers — it asks a person to recall that DeepSeek's endpoint is
 * `api.deepseek.com` and its model is `deepseek-chat`, with no feedback until something fails.
 *
 * ⚠️ NOBODY KNOWS THAT, AND NOBODY SHOULD HAVE TO. Choosing who you pay is one decision;
 * everything else is a lookup, and a lookup is the app's job.
 *
 * ⚠️ AND THE LOCAL PATH ASKS FOR NOTHING AT ALL. If Ollama is running, the panel finds the
 * models already installed and offers them by name. No key, no endpoint, no model string —
 * the minimum possible work is none.
 */

const AI = (() => {
  let providers = [];
  let saved = { base_url: "", model: "", configured: false };
  let busy = false;

  const el = (id) => document.getElementById(id);

  function note(msg, kind) {
    const m = el("ai-msg");
    if (!m) return;
    m.className = "setup-msg" + (kind ? " " + kind : "");
    m.textContent = msg || "";
  }

  async function load() {
    const r = await api("/api/llm/providers");
    if (!r.ok) {
      note(`Could not read the model settings: ${r.error || "the engine is not responding"}`,
           "err");
      return;
    }
    providers = r.providers || [];
    saved = { base_url: r.base_url || "", model: r.model || "", configured: !!r.configured };
    // ⚠️ PRE-SELECT WHATEVER IS ALREADY SAVED, matched by endpoint. Starting on "OpenAI" when
    // the user is connected to DeepSeek would make the panel look like it had forgotten them.
    let current = providers.find((p) => p.base_url && p.base_url === saved.base_url);
    if (!current) {
      const local = providers.find((p) => p.local && p.available);
      current = local || providers.find((p) => p.id === "openai");
    }
    render(current ? current.id : "custom");
  }

  function render(selectedId) {
    const host = el("ai");
    if (!host) return;
    host.textContent = "";

    const h = document.createElement("div");
    h.className = "setup-title";
    h.textContent = "Use a model for answers";
    host.appendChild(h);

    const sub = document.createElement("div");
    sub.className = "setup-sub";
    sub.textContent = "Optional. Searching works without this. A model turns results into a "
                    + "written answer, and its snippets are sent to whoever you choose below.";
    host.appendChild(sub);

    const msg = document.createElement("div");
    msg.id = "ai-msg";
    msg.className = "setup-msg";
    host.appendChild(msg);

    // ---------------------------------------------------------------- provider
    const list = document.createElement("div");
    list.className = "setup-list";
    for (const p of providers) {
      const row = document.createElement("label");
      row.className = "setup-row ai-prov" + (p.available ? "" : " unavailable");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "aiprov";
      radio.checked = p.id === selectedId;
      radio.onchange = () => render(p.id);
      const name = document.createElement("span");
      name.className = "setup-name";
      name.textContent = p.label;
      const det = document.createElement("span");
      det.className = "setup-path";
      // ⚠️ THE COUNT IS SHOWN, NOT JUST A TICK. "Found 3 models" is what makes picking the local
      // option feel like it did something; a bare radio button leaves the user guessing whether
      // their server was seen at all.
      det.textContent = p.local
        ? (p.available ? `${p.models.length} model${p.models.length > 1 ? "s" : ""} found \u00b7 stays on this Mac`
                       : "not running")
        : (p.needs_key ? "needs an API key" : "");
      row.append(radio, name, det);
      list.appendChild(row);
    }
    host.appendChild(list);

    const cur = providers.find((p) => p.id === selectedId);
    if (!cur) return;

    // ---------------------------------------------------------------- the one field
    const form = document.createElement("div");
    form.className = "setup-ai-form";

    if (cur.local) {
      if (cur.available) {
        const pick = document.createElement("label");
        pick.className = "setup-ai-row";
        const t = document.createElement("span");
        t.textContent = "Model";
        const sel = document.createElement("select");
        sel.id = "ai-model";
        for (const m of cur.models) {
          const o = document.createElement("option");
          o.value = m;
          o.textContent = m;
          if (m === saved.model) o.selected = true;
          sel.appendChild(o);
        }
        pick.append(t, sel);
        form.appendChild(pick);
      } else {
        const warn = document.createElement("div");
        warn.className = "setup-sub";
        // ⚠️ A REASON AND A NEXT STEP. "Not running" alone leaves the user with a dead option
        // and no idea whether it is broken or simply switched off.
        warn.textContent = `Nothing is listening on ${cur.base_url}. Start the app that serves `
                         + `it \u2014 Ollama or LM Studio \u2014 and reopen this panel.`;
        form.appendChild(warn);
      }
    } else {
      const keyRow = document.createElement("label");
      keyRow.className = "setup-ai-row";
      const kt = document.createElement("span");
      kt.textContent = "API key";
      const ki = document.createElement("input");
      ki.id = "ai-key";
      // ⚠️ WRITE-ONLY. It shows the last four characters of whatever is saved and never the key
      // itself — a settings screen that displays a secret turns every screenshot and every
      // devtools session into a leak.
      ki.type = "password";
      ki.placeholder = saved.configured ? "saved \u2014 leave blank to keep" : "paste your key";
      ki.spellcheck = false;
      keyRow.append(kt, ki);
      form.appendChild(keyRow);
    }

    // ⚠️ ENDPOINT AND MODEL APPEAR ONLY FOR "Something else". For a known provider they are a
    // lookup, and showing them anyway is how the first version asked for three fields to
    // accomplish one decision.
    if (cur.id === "custom") {
      const mk = (label, id, value, ph) => {
        const row = document.createElement("label");
        row.className = "setup-ai-row";
        const t = document.createElement("span");
        t.textContent = label;
        const i2 = document.createElement("input");
        i2.id = id; i2.type = "text"; i2.value = value || ""; i2.placeholder = ph || "";
        i2.spellcheck = false;
        row.append(t, i2);
        return row;
      };
      form.appendChild(mk("Endpoint", "ai-base", saved.base_url, "https://api.example.com/v1"));
      form.appendChild(mk("Model", "ai-model-text", saved.model, "model-name"));
    }
    host.appendChild(form);

    // ---------------------------------------------------------------- actions
    const foot = document.createElement("div");
    foot.className = "setup-foot";
    const go = document.createElement("button");
    go.className = "primary";
    // ⚠️ ONE BUTTON THAT SAVES AND TESTS. The old panel had separate Save and Test buttons, and
    // a Test that ran against an UNSAVED key — the most confusing possible outcome, because the
    // form looks correct and the test fails.
    go.textContent = busy ? "Checking\u2026" : (saved.configured ? "Update and test"
                                                                : "Connect and test");
    go.disabled = busy || (!cur.needs_key && !cur.local) || (cur.local && !cur.available);
    go.onclick = () => connect(cur);
    const off = document.createElement("button");
    off.textContent = "Turn off";
    off.style.display = saved.configured ? "" : "none";
    // ⚠️ A WAY OUT. A secret with no way to remove it is one people regret saving.
    off.onclick = async () => {
      await api("/api/llm", { method: "POST",
                              body: { api_key: "-", base_url: "", model: "" } });
      saved = { base_url: "", model: "", configured: false };
      await load();
    };
    const skip = document.createElement("button");
    skip.textContent = "Close";
    skip.onclick = () => close();
    foot.append(go, off, skip);
    host.appendChild(foot);
  }

  async function connect(p) {
    busy = true;
    note("Saving\u2026");
    const body = { base_url: p.base_url, model: p.model };
    if (p.id === "custom") {
      body.base_url = (el("ai-base") || {}).value || "";
      body.model = (el("ai-model-text") || {}).value || "";
    } else if (p.local) {
      body.model = (el("ai-model") || {}).value || p.model;
      // ⚠️ A LOCAL SERVER NEEDS NO KEY, but the API refuses to save without one because a
      // remote provider without a key is a configuration that cannot work. The sentinel says
      // "deliberately none" rather than the field being left ambiguous.
      body.api_key = "local";
    } else {
      body.api_key = ((el("ai-key") || {}).value || "").trim();
    }

    const save = await api("/api/llm", { method: "POST", body });
    if (!save.ok) {
      busy = false;
      note(`Could not save: ${save.error}`, "err");
      render(p.id);
      return;
    }
    note("Saved. Testing the connection\u2026");
    const test = await api("/api/llm/test");
    busy = false;
    if (test.ok) {
      saved = { base_url: body.base_url, model: body.model, configured: true };
      const q = el("q");
      if (q) q.focus();
      close();
      return;
    }
    // ⚠️ THE FAILURE STAYS ON SCREEN WITH THE REASON. Closing the panel and writing the error
    // somewhere else is how a user ends up believing it worked.
    note(`Saved, but the connection failed: ${test.reason}`
         + (test.detail ? ` (${String(test.detail).slice(0, 90)})` : ""), "err");
    render(p.id);
  }

  function open() {
    el("ai-overlay").classList.add("show");
    load();
  }
  function close() {
    el("ai-overlay").classList.remove("show");
    note("");
    // ⚠️ THE SEARCH BOX IS TOLD TO RE-CHECK. The "answer" toggle is hidden until a key is
    // saved, and without this it would only appear on the next launch — so connecting a model
    // would look like it had not worked, which is the exact confusion this panel exists to
    // remove. An event rather than a direct call, so ai.js does not need to know how app.js is
    // organised.
    window.dispatchEvent(new CustomEvent("ds:model-changed"));
    const q = el("q");
    if (q) q.focus();
  }
  const isOpen = () => el("ai-overlay")?.classList.contains("show");

  return { open, close, isOpen, reload: load };
})();
