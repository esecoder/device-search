# device-search

**Agentic search over your own device.** Type a fragment, a filename, or a question — it finds
where the thing is.

```bash
ds setup --mode curated && ds index
ds search 'InputLayer(shape=(784,))'
ds search 'where do I configure the embedding dimension'
```

---

## The idea in one line

Most "search your files" tools pick one retrieval method and apply it to everything. That fails,
and it fails on the simplest query you would actually type:

> `InputLayer(shape=(784,))` is **24 characters that either exist in a file or do not.**
> An embedding model returns *"things about Keras input layers"* and leaves the actual line
> unretrieved. Semantic search answers questions about **meaning**; this is a question about
> **text**.

So the tool runs four backends and **the query decides which ones run**:

| Backend | Answers | Example query |
|---|---|---|
| **exact** | *are these characters in a file?* | `InputLayer(shape=(784,))` |
| **keyword** (BM25) | *which file is about these words?* | `bm25 reranker` |
| **path** | *where is the file called X?* | `retrievers.py` |
| **semantic** (local embeddings) | *what is this concept near?* | `how do I check video moves smoothly` |

⚠️ **That routing is the agentic part.** Not "run everything and merge" — a fixed three-stage
pipeline is not an agent. The classifier reads the query, picks the tools, and if the first pass
finds nothing, **broadens** to the ones it skipped.

---

## ⚠️ The finding that shaped the design

A control query — `ZZQX_NOT_IN_ANY_FILE_9931`, which is definitely not on disk — returned
**thirteen confident results.** Semantic search always returns its top-k, because cosine
similarity is *relative*.

The obvious fix is a minimum-similarity threshold. **The measurement says that cannot work:**

| query | top-1 similarity |
|---|---|
| `how do I check whether video moves smoothly` | 0.673 ← real |
| `InputLayer(shape=(784,))` | 0.674 ← real |
| `asdkjhqwlekjhasd qwoiuqwoiu` | **0.649** ← garbage |
| `where is the BM25 implementation` | 0.632 ← real |
| `ZZQX_NOT_IN_ANY_FILE_9931` | 0.615 ← garbage |

⚠️ **The best garbage query outscores the worst real one. The boundary is negative.**
This is embedding anisotropy — bge-small maps everything into a narrow cone, so every score
lands in 0.5–0.7 whether or not the document is relevant.

**The fix is structural, not a number.** Output is split into two claims that were never the
same claim:

```
MATCHES — your words are in these files (2)
  ...evidence the thing exists...

CLOSEST BY MEANING — no word match, ranked by similarity (4)
  ⚠️ These are NOT evidence the thing exists.
```

Only lexical hits count as "found". Semantic hits are offered as leads, labelled as leads.
**A confident wrong answer is worse than an honest miss here, because you stop looking.**

---

## Install

```bash
cd ~/WebstormProjects/device-search
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

⚠️ **The base install is TORCH-FREE (~264 MB)**, and the code defaults to ONNX to match.
The previous version of `requirements.txt` installed `sentence-transformers`, which downloads
~2 GB of PyTorch that nothing runs unless you ask for it.

| Install | Size | Adds |
|---|---|---|
| `pip install -r requirements.txt` | **~264 MB** | text search, ONNX embeddings |
| `+ pip install -r requirements-media.txt` | ~110 MB more | PDF and OCR |
| `+ pip install -r requirements-torch.txt` | ~2 GB | the PyTorch path, for GPU or large indexing runs |

⚠️ **`onnxruntime` is pinned below 1.20 in `requirements.txt`, and the pin is not cosmetic.**
1.23+ is built for macOS 13.4 and **fails to load on macOS 13.0**. An unpinned install produces
an app that dies at import with a symbol error before it can explain itself.

The embedding model (~130 MB) downloads on first index. Without it, exact + keyword + path still
work and the CLI says so.

Put `ds` on your PATH, or call it directly:
```bash
export PATH="$PWD/bin:$PATH"     # then: ds search "..."
# or
./bin/ds search "..."
```

## Use

```bash
ds setup --mode curated              # Documents, Desktop, Downloads, code folders
ds setup --mode everything           # ⚠️ ENTIRE home directory, NO exclusions — reads the warning
ds setup --roots ~/work ~/notes      # exactly these folders

ds index                             # crawl + build; safe to re-run (incremental)
ds index --no-semantic               # fast, fully offline, no model download
ds search 'some exact string'
ds search 'a question about a concept'
ds search 'retrievers.py' -k 5 --json
ds search 'something' --llm          # optional LLM rerank (see privacy below)
ds stats                             # what is actually indexed
ds secrets-report                    # recognised secrets in the index, redacted
```

⚠️ **Run `ds stats` before concluding a file does not exist.** A miss from a partial index and a
miss from a full one are different facts, and only the tool knows which one you just had.

## Run the desktop app

⚠️ **Requires the venv to exist first** (see Install above) — the shell launches the Python
daemon itself, and cannot if there is no interpreter to launch.

```bash
cd ~/WebstormProjects/device-search

./bin/ds setup --roots ~/Documents ~/work   # ⚠️ YOUR folders, not a test corpus
./bin/ds index                              # build the index

cd src-tauri
cargo run                                   # first build compiles ~400 crates, then ~5s
```

A **tray icon** appears. Press **`Cmd+Shift+Space`** (Windows/Linux: `Ctrl+Shift+Space`) to
summon the search box. Left-click the tray icon does the same. `Esc` dismisses; the app stays
running in the tray.

⚠️ **NOT `Cmd+Space`** — that is Spotlight's, and overriding a well-known system shortcut in an
open-source app is a hostile default.

⚠️ **If the hotkey does nothing on macOS**, grant Accessibility permission in
System Settings → Privacy & Security. A global hotkey is an OS-level capability and macOS gates
it. ⚠️ **This is unverified** — the app was built on a machine where the window could not be
seen, so the layout and the hotkey firing are unchecked.

### What needs to be running

Two processes:

| Process | Started by | Purpose |
|---|---|---|
| `python -m device_search.server` | **the shell starts it automatically** | the search engine, warm model, HTTP on 127.0.0.1:8734 |
| the Tauri app | you | tray icon, hotkey, window |

If a search says *"daemon unreachable"*, start it manually and read the error:
```bash
./bin/ds                    # if this fails, the venv is the problem
./.venv/bin/python -m device_search.server
```

## Privacy

⚠️ **A search index is a copy of your files in a second location.** That is the point, and it is
also the risk.

- The index lives at **`~/.device-search/`** (mode `700`), never in this repo.
- **`--llm` sends file snippets to an API.** It is off by default.
- ⚠️ **Before any snippet leaves the process, it is scanned for recognised secret formats**
  (private keys, `sk-…`, `AKIA…`, `ghp_…`, JWT, `.env` assignments, connection strings). A match
  is **dropped, not redacted**, and the drop is reported.
- ⚠️ **That interlock is not a guarantee.** A regex cannot recognise an unlabelled password in a
  notes file. It stops the catastrophic, obvious cases. **Anything it cannot recognise is your
  responsibility** — which is why `everything` mode prints what it will do and makes you type
  the word before it starts.

## Verified

Measured on the `11-rag` track (13 documents, 399 vectors):

| Test | Result |
|---|---|
| Exact code fragment `extract_calls(sources, syms)` | ✅ found, correct file **and line number** |
| Filename `retrievers.py` | ✅ routed to path, ranked #1 |
| Concept question (no shared keywords) | ✅ routed to semantic, correct file #1 |
| **Control: a string not on disk** | ✅ **exit 1, "NO LEXICAL MATCH"** — no false confidence |

⚠️ **Not verified:** behaviour on a 500k-file home directory. The crawl is written for it
(permission errors are skips, symlinks are not followed, every skip is counted) but it has only
been run on a 13-file corpus. **Index a curated set first** and check `ds stats` before trusting
`everything` mode.

⚠️ **Also not verified:** the `--llm` reranker's happy path — it needs an API key. The secret
interlock is rule-based and tested; the ranking quality is not.

---

## What is deliberately not here

- **No vector database.** SQLite holds this corpus comfortably. A server-backed DB adds an
  install, a port and a failure mode, and buys nothing until you are past millions of chunks.
- **No LangChain / LlamaIndex.** The routing logic is ~80 lines. Reading it is the point.
- **No FTS5.** SQLite ships it and I reached for it first — then could not verify it was
  present, because `sqlite3` compiled without FTS5 is common. ⚠️ **A backend that is silently
  absent is worse than one that is slower**, so BM25 is implemented in NumPy and always works.
- **No model-based query classifier.** The classifier is rule-based: deterministic, testable,
  no key. ⚠️ It is also genuinely worse than a model on ambiguous queries, and that is stated
  rather than implied.
