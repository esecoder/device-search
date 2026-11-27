# Notes

The short version of how this app works and what it must never do. Read this before
changing anything. It exists because the same mistakes kept being rediscovered.

---

## The shape

```
daemon (Python, port 8734)  ←  the only thing that knows anything
        ↑ HTTP + token
interface (webview)         ←  shows what the daemon reports, never guesses
```

**The interface never computes state.** If it needs to know whether the index is
stale, how many documents there are, or what stage startup is at, it asks.

---

## The one rule

**Never tell the user something is missing without telling them what to do about it.**

Every message about absent data ends in a button that opens the panel that fixes it.

---

## Startup

- The daemon binds its socket **first**, then opens the engine, so it can report
  its own stage while starting. Roughly 15 seconds.
- **`/api/health` is the only liveness signal.** It must never raise and must never
  block. Expensive work belongs on a background thread.
- **There is exactly one "not ready" indicator**: `#boot`, a translucent overlay
  with a spinner and one line of text. It blocks input.
- **Do not add a second one.** No banners, no status lines, no stage lists in the
  results area. One overlay.
- **The overlay is a scrim, not a curtain.** Roughly `rgba(28,28,30,.34)` and **no
  backdrop blur** — a blur averages everything underneath into a flat field and the
  result is opaque however low the opacity is. The search bar must stay visible
  through it, because it is what the user is waiting to use.
- **The spinner sits below the search row**, not centred on the whole window.
- **A field that is not known yet says "starting", never "off".** The daemon is
  ready before `warm()` finishes, so `semantic` is briefly unknown. An empty value
  rendered as "semantic off" — a claim about the feature rather than the loading.
  The interface also re-polls until it resolves, because one reading of a value
  that changes is a snapshot presented as a fact.

---

## Long operations

| Operation | Cost | Consent |
|---|---|---|
| Meaning index | ~18 hours | offered, never automatic |
| Image descriptions | ~45 min | offered |
| Video frames | ~7 min | offered |
| Audio transcripts | ~7 min | offered |
| Code index (trigram) | 9 min, 6.8 GB | offered on first fragment query |
| Path index | 60 s, 1.3 GB | built with the index |

**Auto-repair acts only when vectors are WRONG, not when they are merely
incomplete.** A coverage gap is a fact to show, not damage to fix silently.

---

## Performance invariants

Measured on 866,000 documents. If a change makes any of these worse, it is wrong.

| | |
|---|---|
| Search, any kind | **under 2 s** |
| Health | **under 0.2 s** |
| Folder list | **under 1.5 s** |
| Startup | **under 30 s** |

**Rules that produced those numbers:**

- `COUNT(*)` must go through an index, never the table (rows hold document text).
- `LIKE 'prefix%'` cannot seek — use a range comparison.
- A covering index for any query that needs a second column.
- Anything that reads every document's text runs **once**, is **cached to disk**,
  and **never on the request path**.
- Python threads share the GIL. A CPU-bound loop over 800,000 rows blocks every
  other request in the process.

---

## Retrieval

**A LITERAL FRAGMENT MUST LEAD WITH `exact`.** Two bugs made `InputLayer(shape=(784,))`
return files named `shape`:

  `_weights["path"] = 4.0` while exact stayed at the default 1.0 — a filename outranked
  a literal match four to one.

  The primary-backend choice tested `_path_top >= 3.0` BEFORE `_kind == "code"`, and
  path_search gives 3.0 for a name starting with one token. A folder called `shape`
  scored 12.0 ("a directory whose name is the query") against a five-token fragment.

The rules now: exact is weighted 4.0, a multi-token fragment leads with exact unless a
file is named the whole query, and a question leads with semantic (a question sharing
a word with a directory name is still a question).

**Quoted queries are literals.** The quotes are stripped before searching, they never
broaden, they never grep unindexed files, and a miss says so in words. Searching for
the quote characters is why quoted and unquoted returned the same list.

| Backend | For | Cannot |
|---|---|---|
| path | `config.py` | content |
| keyword (FTS5) | `retry backoff` | paraphrases |
| exact (trigram) | `InputLayer(shape=(784,))` | anything conceptual |
| semantic | `how do I configure X` | rare strings |
| metadata | `files over 100 MB` | content |

**Media is a separate tier**, never fused with BM25. Scores are not comparable.

**Media is drawn AFTER the file list, never before.** A filename-shaped query is
answered by the filename; pictures of that word are a second observation.

**Media always runs.** A keyword gate was tried and removed: it was a regex guessing
at intent and it missed queries that genuinely appear inside an image. A measured
score floor does the work instead - on this corpus SigLIP runs 0.06 (nothing) to 0.17
(a real match), so the floor is 0.145.

**No horizontal scrolling.** Paths and snippets wrap. A path has no spaces, so it
needs `overflow-wrap: anywhere` or it forces the panel wider than the window.

**`overflow-y: auto` silently makes `overflow-x: auto`.** Locking `html`, `body` and
`#box` was not enough: `#results` scrolled sideways on its own, and `width:
max-content` on `.row` let a long path push it wide. If a scrollbar appears, look for
the nearest `overflow-y` and set `overflow-x: hidden` beside it.

**Coverage gaps are reported per query.** A filename search does not need the image
index and must not be offered it.

---

## Storage

```
~/.device-search/
├── index.db        documents + FTS5 + paths_tri + symbols/edges
├── index.db.tri    code-fragment trigram (optional)
├── images/         SigLIP vectors
├── video/          frame vectors
├── index.status.json
└── daemon.log      line-buffered, or a hung start writes nothing
```

- **Every schema change is idempotent** and runs once.
- **Indexes are never created by accident.** A missing index file must not be
  silently replaced with an empty one.
- **Log output is line-buffered.** An empty log should mean nothing happened, not
  that it was never flushed.

---

## Before saying "done"

1. **Run `npm test`** — 32 checks, including Python syntax.
2. **Launch the app and wait for it to actually be ready.** A green build is not a
   working app. The daemon log is the place to look.
3. **Search something.** `config.py` and `InputLayer(shape=(784,))`.
4. **Check the daemon log is empty.** No tracebacks.
5. **Never `git checkout --` a file you have edited without committing first.**

---

## Mistakes already made, so they are not made again

- `executemany` with a list of strings — it treats each as a parameter set.
- Deleting the trigram index to save disk, which broke the headline feature.
- `if cand:` treating "no matches" as "the index is broken", then scanning 8 GB.
- A health handler that recomputed 6 GB of text lengths on every poll.
- A 30-second timeout for a 105-second startup.
- Reading an empty log as "nothing happened" instead of "nothing was written".
- Building a second loading indicator instead of fixing the first.
- Reporting a bug from truncated output without checking the full value.
