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

**That routing is the agentic part.** Not "run everything and merge" — a fixed three-stage
pipeline is not an agent. The classifier reads the query, picks the tools, and if the first pass
finds nothing, **broadens** to the ones it skipped.

---

## The finding that shaped the design

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

**The best garbage query outscores the worst real one. The boundary is negative.**
This is embedding anisotropy — bge-small maps everything into a narrow cone, so every score
lands in 0.5–0.7 whether or not the document is relevant.

**The fix is structural, not a number.** Output is split into two claims that were never the
same claim:

```
MATCHES — your words are in these files (2)
  ...evidence the thing exists...

CLOSEST BY MEANING — no word match, ranked by similarity (4)
  These are NOT evidence the thing exists.
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

**The base install is TORCH-FREE (~264 MB)**, and the code defaults to ONNX to match.
The previous version of `requirements.txt` installed `sentence-transformers`, which downloads
~2 GB of PyTorch that nothing runs unless you ask for it.

| Install | Size | Adds |
|---|---|---|
| `pip install -r requirements.txt` | **~264 MB** | text search, ONNX embeddings |
| `+ pip install -r requirements-media.txt` | ~110 MB more | PDF and OCR |
| `+ pip install -r requirements-torch.txt` | ~2 GB | the PyTorch path, for GPU or large indexing runs |

**`onnxruntime` is pinned below 1.20 in `requirements.txt`, and the pin is not cosmetic.**
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
ds setup --mode everything           #  ENTIRE home directory, NO exclusions — reads the warning
ds setup --roots ~/work ~/notes      # exactly these folders

ds index                             # crawl + build; safe to re-run (incremental)
ds index --no-semantic               #  START HERE on a real corpus — see the timings below
ds search 'some exact string'
ds search 'a question about a concept'
ds search 'retrievers.py' -k 5 --json
ds search 'something' --llm          # optional LLM rerank (see privacy below)
ds stats                             # what is actually indexed
ds secrets-report                    # recognised secrets in the index, redacted
```

**Run `ds stats` before concluding a file does not exist.** A miss from a partial index and a
miss from a full one are different facts, and only the tool knows which one you just had.

## How long indexing takes, and why

**Two phases with wildly different costs.** The crawl and text extraction are fast; the
embedding phase is not, and it dominates everything.

| Corpus | Text phase | Embedding phase (ONNX) | Embedding phase (torch) |
|---|---|---|---|
| 117 documents | **0.1 s** | 12 min | 3 min |
| **5,700 documents** (a real Documents+Desktop+Downloads) | ~2 min | **~6.5 hours** | **~1.7 hours** |
| 100,000 documents | ~30 min | **~115 hours** | ~30 hours |

**The measured rate is 8.2 chunks/sec on ONNX and 31.4 on torch**, on CPU, from 5,726 real
chunks. **Neither runtime makes CPU indexing at this scale pleasant — the difference is a
factor of four, not an order of magnitude.**

**SO RUN THIS FIRST:**
```bash
./bin/ds index --no-semantic
```
It finishes in seconds and gives you **exact string matching, BM25 and filename search over
everything** — which covers the query that motivated this project. Semantic search is the thing
you add afterwards, overnight, or on a machine with a GPU.

**AND IF YOU RUN THE FULL INDEX, DON'T SIT AND WATCH IT.** It will look hung after a few
minutes of no output, because the embedding phase reports every 5,000 documents. Run it under
`nohup` and check the log:
```bash
nohup ./bin/ds index > /tmp/index.log 2>&1 &
tail -f /tmp/index.log
```
**It is incremental**, so interrupting it is survivable — but the VECTOR phase is not
resumable yet: an interrupted embedding run leaves the previous `vectors.npy` in place, and
nothing warns you that it is stale. Check with `./bin/ds stats`.

## Run the desktop app

**Requires the venv to exist first** (see Install above) — the shell launches the Python
daemon itself, and cannot if there is no interpreter to launch.

```bash
cd ~/WebstormProjects/device-search

./bin/ds setup --roots ~/Documents ~/work   #  YOUR folders, not a test corpus
./bin/ds index                              # build the index

cd src-tauri
cargo run                                   # first build compiles ~400 crates, then ~5s
```

A **tray icon** appears. Press **`Cmd+Shift+Space`** (Windows/Linux: `Ctrl+Shift+Space`) to
summon the search box. Left-click the tray icon does the same. `Esc` dismisses; the app stays
running in the tray.

**NOT `Cmd+Space`** — that is Spotlight's, and overriding a well-known system shortcut in an
open-source app is a hostile default.

**If the hotkey does nothing on macOS**, grant Accessibility permission in
System Settings → Privacy & Security. A global hotkey is an OS-level capability and macOS gates
it. **This is unverified** — the app was built on a machine where the window could not be
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

**A search index is a copy of your files in a second location.** That is the point, and it is
also the risk.

- The index lives at **`~/.device-search/`** (mode `700`), never in this repo.
- **`--llm` sends file snippets to an API.** It is off by default.
- **Before any snippet leaves the process, it is scanned for recognised secret formats**
  (private keys, `sk-…`, `AKIA…`, `ghp_…`, JWT, `.env` assignments, connection strings). A match
  is **dropped, not redacted**, and the drop is reported.
- **That interlock is not a guarantee.** A regex cannot recognise an unlabelled password in a
  notes file. It stops the catastrophic, obvious cases. **Anything it cannot recognise is your
  responsibility** — which is why `everything` mode prints what it will do and makes you type
  the word before it starts.

## Verified

Functional behaviour, measured on the `11-rag` track (13 documents, 399 vectors):

| Test | Result |
|---|---|
| Exact code fragment `extract_calls(sources, syms)` | ✅ found, correct file **and line number** |
| Filename `retrievers.py` | ✅ routed to path, ranked #1 |
| Concept question (no shared keywords) | ✅ routed to semantic, correct file #1 |
| **Control: a string not on disk** | ✅ **exit 1, "NO LEXICAL MATCH"** — no false confidence |

Scale behaviour, measured separately on a **859,569-document corpus (6.9 GB)** — see
[the index section](#the-index-what-fts5-gave-and-what-the-trigram-index-cost):

| Test | Result |
|---|---|
| Keyword search, first call | ✅ **0.002s** (was **351.45s** on the in-process BM25) |
| Index build | ✅ **90s, once ever** — it lives in the database, not rebuilt per restart |
| Fragment search via trigram | ✅ **10.73s → 0.015s** — but see the recommendation to revert it |
| `path` after the trigram move | **1.77s**, still worse than the **0.73s** before any of this |

**Test suites:**

```bash
npm test                              # 32 assertions — the UI and setup paths
python tests/test_fts_fallback.py     # 9 checks — the FTS5 fast path, its fallback, and
                                      #   identifiers containing '_' and '.' on both
```

**The fallback test is not decoration.** The NumPy BM25 fallback turned out to be **dead code
that returned nothing** on exactly the builds it existed to serve, and the assertion that caught
it is `keyword search still returns results on the fallback`.

**Not verified:** the crawl has not been exercised on a large *home* directory specifically.
It is written for one (permission errors are skips, symlinks are not followed, every skip is
counted), and the index work above has been measured at 859,569 documents — but **index a
curated set first** and check `ds stats` before trusting `everything` mode.

**Also not verified:** the `--llm` reranker's happy path — it needs an API key. The secret
interlock is rule-based and tested; the ranking quality is not.

**And the honest state of the trigram index: not finished, and probably not worth it.** Size,
build cost and benefit are all measured above, and the recommendation is to revert it — stated
rather than left for someone to discover from their disk filling up.

---

## What is deliberately not here

- **No vector database.** SQLite holds this corpus comfortably. A server-backed DB adds an
  install, a port and a failure mode, and buys nothing until you are past millions of chunks.
- **No LangChain / LlamaIndex.** The routing logic is ~80 lines. Reading it is the point.
- **No FTS5 *as the only* keyword path.** FTS5 *is* used — see
  [the index section](#the-index-what-fts5-gave-and-what-the-trigram-index-cost) below — but the
  NumPy BM25 is kept as a **runtime-checked fallback**, because `sqlite3` compiled without FTS5
  is common on macOS system Python. **A backend that is silently absent is worse than one
  that is slower**, which is why the check is at runtime and the degradation is logged rather
  than assumed.
- **No model-based query classifier.** The classifier is rule-based: deterministic, testable,
  no key. It is also genuinely worse than a model on ambiguous queries, and that is stated
  rather than implied.

---

## The index: what FTS5 gave, and what the trigram index cost

**This section corrects an earlier claim in this README.** It used to say, under *"What is
deliberately not here"*, that there was no FTS5 — because `sqlite3` compiled without FTS5 is
common on macOS system Python, and *"a backend that is silently absent is worse than one that
is slower."* **That reasoning was sound and the conclusion was wrong.** The right answer was to
check at runtime and degrade, not to avoid the fast path. The code does that now; the README
did not catch up.

### FTS5 is the primary keyword backend

At **859,569 documents (6.9 GB)**, the first keyword query took **five minutes fifty-one
seconds**:

```
                before      after
exact            4.09s       (see below)
path             0.79s       1.77s
keyword        351.45s       0.002s     ← 'screenshot', 400 hits
```

The Python BM25 *caches*, so the second query was 0.06 s — **but the first call happens on every
daemon restart**, and its postings live in RAM. That is what the user experiences as the app
being broken.

FTS5 keeps the index **on disk** and maintains it **with triggers**, which the in-process
postings could not do. It is an **external-content** table (`content='documents'`) so the text
is not stored twice — at 6.9 GB a duplicate copy is the difference between an index and a
second corpus. It uses the **same BM25 formula and the same `k1` and `b`** as the Python path,
so the two agree rather than quietly disagreeing, and the Python version stays as a
**runtime-checked fallback** for builds without FTS5 (`tests/test_fts_fallback.py` pins both
the fallback and its warning).

```
building the index   90s   ONCE, EVER — it lives in the database, not rebuilt per restart
'screenshot'       0.002s   400 hits
'merge conflict'   0.011s   400 hits
'php'              0.004s   400 hits
```

### The first attempt was fast and wrong, and that is the part worth keeping

```
count(docs_fts)   859,569
count(documents)  859,569
raw MATCH             0
```

With `content='documents'` the FTS table is external-content, so `SELECT count(*) FROM docs_fts`
returns the number of rows in the **content** table — not the number of terms indexed. **The
rebuild check compared a number with itself, passed every time, and the rebuild never ran.**

Searches then returned in **0.00 s with zero results**. **Fast, and wrong — and a speed
measurement alone would have called it a success.** Fixed by counting `docs_fts_data`, which is
the actual index: a fresh table has a handful of structural rows, a built one has hundreds of
thousands.

### Three more silent bugs in the FTS work, each found only by testing a fresh database

1. **No triggers.** An external-content FTS5 table stores no text of its own, so it cannot know
   a row changed. **MEASURED:** a document inserted after the index was built was invisible to
   keyword search — before a restart *and* after one. Fixed with SQLite's documented
   three-trigger pattern (insert / delete / update, the update deleting then re-inserting,
   because a delete without the old text leaves stale terms that match forever).
2. **Eager but too early.** The triggers were created at open — but *before*
   `executescript(SCHEMA)`, so `content='documents'` named a table that did not exist, the
   `CREATE` failed, `_fts_ok` stayed `False`, and keyword search fell back to the slow Python
   path **forever, without saying so.**
3. **`.isalnum()` dropped every identifier with an underscore or a dot.** **MEASURED:** a raw
   `MATCH` for `unique_token_xyz` and `db_acl.php` returned their rows, and the search returned
   **zero** — because `"_".isalnum()` is `False` and the token was filtered before the query was
   built. **This is a file search tool:** underscores and dots are in most filenames and most
   code identifiers, so the filter removed exactly the tokens the product exists to find, and it
   did so silently, by returning an empty result rather than an error. Fixed by **quoting** the
   tokens instead — FTS5 treats a quoted token as a literal string, so punctuation cannot be
   parsed as syntax. Filtering was never needed.

### And a fourth, found while writing this section: the fallback was dead code

`_build_postings()` — the function that builds the NumPy BM25 index — **had no call sites
anywhere in the codebase.** So on a build without FTS5, the fallback ran with `_n_docs == 0`,
hit its own guard, and returned `[]` for every query, forever.

```
two documents indexed:
  FTS5 path     : keyword_search("quick") -> 1 hit
  fallback path : keyword_search("quick") -> 0 hits   ← silently nothing
```

**An empty result is the worst possible failure signal for this tool**, because a file search
that finds nothing reads as *"that file does not exist"* rather than as *"a backend is missing"*.
So the fallback was **not slow — it was broken**, and the fallback was the entire reason this
project originally refused to depend on FTS5. **The argument for the reimplementation rested on
a path that did not work.** Fixed, and `tests/test_fts_fallback.py` now asserts that keyword
search returns results on the fallback path.

### The trigram index: built, measured, and it should probably be reverted

`exact` exists for fragments — `InputLayer(shape=(784,))` is not a concept, it is 24 characters
that exist or do not — and a word tokenizer cannot help, because `(784,)` is not a word and
`shape=(784` spans three. A trigram tokenizer indexes every three-character sequence, so any
substring of length ≥ 3 becomes a token lookup instead of a scan of 6 GB.

```
InputLayer           10.73s -> 0.015s
shape=(784                   -> 0.004s
db_acl.php                   -> 0.002s
```

**But adding an index is not local, and the honest measured result is not a clean win:**

```
                          before      after
exact (fragment)          10.70s      0.015s     the thing it was built for   ✅
path                       0.73s      9.16s      12x SLOWER, never mentioned
index.db                    8.6GB     19.4GB     11GB of trigram data
live search                  ~2s        ~13s     NET WORSE
```

A trigram index is roughly **twice the size of the text it indexes**, and inside the same file
it interleaves with `documents`, so `path_search`'s `LIKE` scan reads scattered pages. Fixed by
**moving it into its own file** (`content=''`, contentless, because FTS5 cannot take content
from another database and `exact_search` only ever asks for rowids):

```
index.db          19.4GB -> 8.2GB
index.db.tri               8.3GB      its own file
path_search        9.16s -> 1.77s
```

**The cost, paid knowingly:** triggers cannot cross database files, so this index has to be
told about changes explicitly from `add_many` — and **two bugs made that sync dead code**, both
found only by testing it. One was a **circular dependency**: `tri_add` refused to add unless
`_tri_ready` was set, and nothing sets `_tri_ready` except a populated index — so on a fresh
database, **nothing was ever added, ever.** The other: `DELETE FROM docs_tri` on a contentless
table is rejected outright (`cannot DELETE from contentless fts5 table`).

**AND THE HONEST RECOMMENDATION IS TO REVERT IT.**

```
size       8.3GB   for an 8.2GB corpus    — it nearly doubles the disk
build      >12min  for 859,569 documents  — on a machine the user is using
benefit    10.7s → 0.015s on FRAGMENT queries, which are the minority of searches
```

Eight gigabytes and twelve minutes to speed up a query type most searches are not is a bad
trade, and **the right time to say so is before it ships rather than after a user's disk fills.**
The routing fix that came with it — running `exact` only where it is genuinely needed, because a
query of ordinary words is already answered by `keyword` in milliseconds — is the real win, and
it cost nothing:

```
screenshot      4.88s -> 1.25s
merge conflict  7.19s -> 0.93s
db_acl.php      7.35s -> 0.81s
```
