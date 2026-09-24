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

`sentence-transformers` is optional — without it, exact + keyword + path still work and the CLI
says so. The model (~130 MB) downloads on first index.

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
