# Manual test plan

Every capability, a query that exercises it, and what a correct answer looks like.
Work top to bottom. A row is only passed when the result matches the expectation, not
merely when something appears.

**Record the time next to each.** Speed is a feature here and half these tests exist
to catch it regressing.

---

## Preconditions

Run these first. If any is wrong, the rest of the plan tests the wrong thing.

| Check | Command | Expected |
|---|---|---|
| Documents indexed | `./.venv/bin/python -m device_search.cli stats` | ~866,000 |
| Code index present | `ls -la ~/.device-search/index.db.tri` | ~6.8 GB |
| Path index present | `sqlite3 ~/.device-search/index.db "SELECT COUNT(*) FROM paths_tri"` | ~866,000 |
| Images embedded | `ls -la ~/.device-search/images` | 1,210 vectors |
| Video frames | `ls ~/.device-search/video` | **absent — see Gaps** |
| Transcriptions | `ds stats \| grep whisper` | **0 — see Gaps** |
| App running | menu bar icon, `⌃⌥Space` | panel appears |

**The video and audio rows are expected to be empty.** Those features have not
been run on this machine. Do not treat an empty media row as a pass until they are.

---

## A. Filename and path

The most common thing anyone types. Everyone uses this, programmer or not.

| # | Query | Correct result | Proves |
|---|---|---|---|
| A1 | `config.py` | `config.py` in the first few | exact basename beats a path fragment deep in a tree |
| A2 | `README.md` | many, varied directories | a common name still returns promptly |
| A3 | `retrievers.py` | the file, near the top | a distinctive name beats a common one |
| A4 | `db_acl.php` | matches on **path** | the router sends a dotted name to `path`, which it once did not |
| A5 | `find db_acl.php` | the same file | a leading verb does not break a filename search |
| A6 | `package.json` | dozens, all called `package.json` | basename ranking above directory noise |
| A7 | `zzzz_not_a_file_zzzz` | **zero results, with a reason** | "no match" is stated, not implied |

**A5 is a regression test.** The path backend once matched the whole query literally,
so `find db_acl.php` searched for that entire string and found nothing.

---

## B. Keyword (FTS5 / BM25)

| # | Query | Correct result | Proves |
|---|---|---|---|
| B1 | `retry backoff` | documents containing both, ranked | BM25 relevance, not just presence |
| B2 | `idempotency key` | prose that discusses the concept | word matching in prose |
| B3 | `ERROR_CONN_4421` | the log line, if present | an identifier survives tokenisation |
| B4 | `"exact phrase from a file"` | only files containing that literal | quotes mean literal, not conceptual |
| B5 | `the and of` | few or none, and fast | stopword-heavy queries do not return everything |

**B4** must return a *strict subset* of B2's behaviour. If quoted and unquoted return
the same list, the quote handling is not working.

---

## C. Exact fragment — the headline feature

This is what a word index cannot do and an embedding cannot do.

| # | Query | Correct result | Proves |
|---|---|---|---|
| C1 | `InputLayer(shape=(784,))` | the line, in a source file | punctuation and no word boundary |
| C2 | `def __init__(self` | many, each containing it | a fragment that is not a token |
| C3 | `useEffect(() => {` | React files | brackets and arrows survive |
| C4 | `v2.1.3-build` | config or log files | dots and dashes |
| C5 | `(7)` | few, and **fast** | under three characters falls back to a scan — check the time |

**C1 is the query in the README.** If it fails, the product's own example fails.
**C5 is expected to be slower.** Note how much. Two characters cannot use a trigram.

---

## D. Semantic — search by meaning

**These are the tests that currently cannot fully pass.** Only ~4,488 of 866,224
documents are embedded, so a correct answer may simply not be embedded yet. See Gaps.

| # | Query | Correct result | Proves |
|---|---|---|---|
| D1 | `how do I configure the embedding dimension` | a config or docs file, **with no shared keywords** | meaning, not words |
| D2 | `stopping a training run early when it stops improving` | early stopping docs | paraphrase |
| D3 | `making the model smaller without losing much accuracy` | quantisation docs | concept with no keyword overlap |
| D4 | `the opposite of overfitting` | regularisation | negation-ish phrasing |
| D5 | `what talks to the database` | connection or ORM files | question phrasing |

**D1–D5 must be checked against the file actually opened**, not just the list order.
A plausible-looking result that does not answer the question is a failure.

---

## E. Questions with an answer

Requires a model in Settings. Without one, the app should say so rather than invent.

| # | Query | Correct result | Proves |
|---|---|---|---|
| E1 | `what does the retry policy say` | a sentence **with `[n]` citations** | grounding |
| E2 | `what is the default batch size` | a number, cited | extraction from a source |
| E3 | `what did I decide about chunk size` | an answer or an honest "not found" | refusal is allowed |
| E4 | `what is the airspeed of an unladen swallow` | **"could not find"**, not a guess | the model does not answer from memory |
| E5 | any question, with no model configured | a message explaining how to add one | no silent empty answer |

**E4 is the most important test in this section.** A system that answers unanswerable
questions from memory is not grounded, and every citation it produces becomes worthless.

---

## F. Metadata — parsed, not searched

| # | Query | Correct result | Proves |
|---|---|---|---|
| F1 | `files over 100 MB` | large files, sorted | size parsing |
| F2 | `files over 10 gb` | few or none, and **does not crash** | unit handling |
| F3 | `what did I change today` | files modified today | date parsing as offsets |
| F4 | `files from last week` | the last seven days | relative dates |
| F5 | `all the .md files` | markdown files | extension parsing |
| F6 | `*.pdf` | PDFs | glob-shaped extension |
| F7 | `files over 100 MB modified today` | **intersection**, not either | filters compose |

**F7 is where this usually breaks**: two filters that each work alone and silently
ignore each other when combined.

---

## G. Multi-hop

| # | Query | Correct result | Proves |
|---|---|---|---|
| G1 | a query whose top result is a file that **names another file** | the second file appears in the results | the reference is followed |
| G2 | the same query, checking the UI | it says a hop happened | the mechanism is visible, not magic |
| G3 | a query with no references | **no hop, no delay** | it does not fire on everything |
| G4 | any query over code | **finishes in under a second** | `self.compiler` is not treated as a file |

**G4 is a regression test.** Dotted identifiers were once followed as paths, at
**8 seconds per false reference and 35 per search** — 235 seconds for one query.

---

## H. Images — describe a picture

| # | Query | Correct result | Proves |
|---|---|---|---|
| H1 | `a screenshot of code` | screenshots | SigLIP image ↔ text |
| H2 | `a chart or graph` | charts | visual category |
| H3 | `a person` | photos with people | semantic matching |
| H4 | `a red car` | cars if any exist | colour and object |
| H5 | `an image containing the word invoice` | OCR matches, listed separately | OCR is a different claim from vision |

**H5 matters**: "found by OCR" and "found by description" are different evidence.
A test that cannot tell them apart does not test either.

---

## I. Video

**Not currently indexed.** Run the media build first, then:

| # | Query | Correct result |
|---|---|---|
| I1 | `a video of a terminal` | video result with a **timestamp** |
| I2 | clicking the result | opens the file at that timestamp |
| I3 | a video with speech | transcribed text findable, with a time |

---

## J. Audio

**0 transcriptions on this machine.** Run transcription first, then:

| # | Query | Correct result |
|---|---|---|
| J1 | a word actually spoken in a recording | the file, with `[M:SS]` |
| J2 | clicking | opens at that moment |
| J3 | a silent recording | **counted as silent, not as "no speech found"** |

---

## K. Scope and filters

| # | Action | Correct result | Proves |
|---|---|---|---|
| K1 | select a folder, search | **only** that folder | scope narrows, never invents |
| K2 | select a folder with nothing in it | zero results **and says the scope** | an empty scope is not an empty disk |
| K3 | clear the scope | results return | clearing restores |
| K4 | scope + a query with no match | says both facts | the two reasons compose |

**K2 is the honesty test.** An empty result inside a narrow folder is not "the file
does not exist" and must not read as though it is.

---

## L. Failure and edge cases

| # | Input | Correct result | Proves |
|---|---|---|---|
| L1 | empty query | idle state, no request | no wasted work |
| L2 | a single space | idle state | trimming |
| L3 | one character `a` | results or a clear limit, promptly | no hang |
| L4 | a 5,000-character query | handled, not a crash | input bounds |
| L5 | quotes only `""` | handled | malformed quotes |
| L6 | emoji `🔍` | zero results, no crash | unicode paths |
| L7 | a path that does not exist | no results, no error dialog | missing folders |
| L8 | daemon stopped mid-search | a clear message | the interface does not hang forever |

---

## Baseline measured before you start

Recorded on this machine, warm, with 866,237 documents indexed. If your run is
materially slower, something has regressed.

| Query | Measured | |
|---|---|---|
| `config.py` | 0.19 s | filename |
| `db_acl.php` | 0.20 s | filename via the code route |
| `find db_acl.php` | 0.11 s | leading verb |
| `package.json` | 0.26 s | common filename |
| `retry backoff` | 0.04 s | keyword |
| `InputLayer(shape=(784,))` | 0.19 s | exact fragment |
| `def __init__(self` | 0.38 s | exact fragment |
| `useEffect(() => {` | 0.32 s | punctuation-heavy fragment |
| `v2.1.3-build` | 0.58 s | fragment with dots and dashes |
| `files over 100 MB` | 0.66 s | metadata |
| `what did I change today` | 0.23 s | metadata |
| `all the .md files` | 0.55 s | extension |
| `*.pdf` | 0.41 s | glob extension |
| `a screenshot of code` | 0.63 s | media |
| `files over 100 MB modified today` | 0.25 s | two filters |
| `ERROR_CONN_4421` | 2.03 s | **zero results** |
| `zzzz_not_a_file_zzzz` | 2.03 s | **zero results** |

The two-second floor on an empty result is `grep_skipped`: having found nothing in
the index, it reads the files that were never indexed, for two seconds, and reports
how many it checked. It is the difference between "not indexed" and "not there".

---

## M. Performance — record the numbers

Run each three times. **The first may be slow (caches); the second and third are the
real figures.**

| Query | Target | Measured |
|---|---|---|
| `config.py` | < 0.5 s | |
| `InputLayer(shape=(784,))` | < 0.5 s | |
| `a screenshot of code` | < 2 s | |
| `how do I configure the embedding dimension` | < 2 s | |
| any question with an answer | < 15 s | |

**A first search over 10 s is a failure even if the second is fast.** A new user's
first query is the one they judge.

---

## N. Interface

| # | Action | Correct result |
|---|---|---|
| N1 | `⌃⌥Space` | panel opens |
| N2 | click elsewhere | panel closes |
| N3 | Escape | panel closes |
| N4 | open Settings | overlay appears |
| N5 | click outside Settings | it closes |
| N6 | tray icon | menu appears |
| N7 | close the window | app stays in the menu bar |
| N8 | quit from the tray | process actually exits |
| N9 | reopen after quit | starts cleanly, no zombie daemon |

**N1 has never been confirmed by a human.** I attempted it with a synthetic
keystroke and the window did not appear; that is not the same as failing, but it is
not a pass either.

---

## Gaps that must be closed before a demo

| | State | Needed for |
|---|---|---|
| **Semantic vectors** | 4,488 of 866,224 | D1–D5, and the banner |
| **Video frames** | 0 of 131 | I1–I3 |
| **Audio transcripts** | 0 of 44 | J1–J3 |
| **Images embedded** | 1,210 of 12,554 | H1–H4, only partly |
| **Signed build** | no | every download |

**Nothing in A, B, C, F, G, K, L or M depends on any of those.** The demo can be built
on those alone — but it should not claim what it has not run.

---

## The five queries worth putting in a video

If only five make the cut, these show the range:

1. **`InputLayer(shape=(784,))`** — the thing Spotlight cannot do at all
2. **`files over 100 MB modified today`** — structured questions, not text search
3. **`a screenshot of code`** — search by description, across media
4. **`how do I configure the embedding dimension`** — meaning, no shared words
5. **`what does the retry policy say`** — an answer with citations

Each is a different mechanism, and each is visibly not a filename lookup.
