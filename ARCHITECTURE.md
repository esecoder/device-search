# How DeviceSearch works

This is the document to read before explaining the project to anyone: what it does,
how a query moves through it, why the pieces are shaped the way they are, and which
parts are measured rather than assumed.

---

## 1. The problem

A computer holds several hundred thousand files. Finding one by name is a solved
problem — Spotlight, `find`, `fd` all do it. Three things are not solved:

1. **A fragment.** `InputLayer(shape=(784,))` is twenty-four characters. It is not a
   word, so a word index cannot find it and an embedding model returns "things about
   Keras layers" without returning the line.
2. **Meaning.** "how do I configure the embedding dimension" shares no words with
   the file that answers it.
3. **Media.** A screenshot, a recording, a video frame. There is no text to index.

Each is a different retrieval problem. One index cannot serve all four, so the app
routes each query to the backends that can answer it.

---

## 2. The shape

```
                    ┌──────────────┐
   query ──────────▶│  classify    │  what kind of question is this?
                    └──────┬───────┘
                           │ picks backends
        ┌──────────┬───────┴────────┬───────────┐
        ▼          ▼                ▼           ▼
     exact      keyword           path       semantic
   (substring)  (FTS5/BM25)   (filename)   (bge-small)
        │          │                │           │
        └──────────┴────────┬───────┴───────────┘
                            ▼
                    ┌──────────────┐
                    │   image      │  SigLIP2: describe a picture
                    │   video      │  frames + timestamps
                    └──────┬───────┘
                           ▼
                 ┌──────────────────┐
                 │  merge + rank    │  (Reciprocal Rank Fusion)
                 └────────┬─────────┘
                          ▼
                 ┌──────────────────┐
                 │  context build   │  token budget, dedupe, 2 per file
                 └────────┬─────────┘
                          ▼
                 ┌──────────────────┐
                 │  answer + cites  │  page numbers, timestamps
                 └──────────────────┘
```

---

## 3. The data pipeline

Ingestion is a chain of decisions, and the order matters:

```
walk → is this text or media? → extract → filter → store → index
```

**Extraction dispatches by cost.** UTF-8 read is cheapest; PDF parsing costs
milliseconds; OCR costs seconds per image; Whisper costs seconds per second of
audio. Each is opt-in when it is expensive, and each shows its estimated cost
before starting.

| Extractor | Produces | Method tag |
|---|---|---|
| text read | the file | `utf8` |
| pdftext | text with `[page N]` markers | `pdftext` |
| RapidOCR | text found inside an image | `ocr` |
| Whisper | transcript with `[M:SS]` markers | `whisper` |

The `method` column is not decoration. "Found in scan.pdf" and "found in scan.pdf
via OCR" are different claims about reliability, and a user checking a hit needs to
know which one they are looking at.

**Position markers are load-bearing.** A citation into a 103-page PDF that says
"page 1" is not a citation. The markers are what let a result say *page 12* or
*2:50*.

---

## 4. Storage

One SQLite database, one file per concern:

```
~/.device-search/
├── index.db           documents + the FTS5 word index
├── index.db.tri       optional trigram index (see §9)
├── vectors.NNNN.npy   embedding shards, one file per ~200 documents
├── vectors.manifest.json
├── images/            SigLIP vectors for pictures
└── video/             SigLIP vectors for sampled frames
```

**SQLite holds documents and the word index; the vectors are separate `.npy`
files.** A vector database would be the right choice at ten million chunks; at
these sizes a memory-mapped array and one matrix multiply is faster and has no
operational cost.

**Sharding exists for resumability, not for speed.** A build that dies partway
leaves the shards it finished, with a manifest that records which documents each
covers. Re-running continues instead of starting over.

---

## 5. Retrieval

### Why four backends

| Backend | Answers | Cannot answer |
|---|---|---|
| exact | are these characters in a file? | anything conceptual |
| keyword (FTS5/BM25) | which file is about these words? | paraphrases |
| path | where is the file called X? | content |
| semantic (bge-small) | what is this concept near? | rare strings, exact code |

The classifier reads the query and picks. `InputLayer(shape=(784,))` has punctuation
and no spaces — exact and keyword. "how do I configure X" is a question — semantic
and keyword. A miss causes one broadening pass over the backends it skipped.

### Why fusion rather than concatenation

Reciprocal Rank Fusion merges ranked lists by position:

```
score(d) = Σ  1 / (k + rank_i(d))        k = 60
```

It needs no score normalisation, which matters because BM25 scores are unbounded
(typically 5–40) and cosine similarities sit around 0.1–0.2. Adding those directly
would put every keyword hit above every semantic one.

### Media is a separate tier

Image and video results are shown under their own heading rather than merged into
the ranked list. They answer a different question — *this picture looks like what
you described* rather than *this file contains what you typed* — and a single
numbered list would imply one ranking that does not exist.

---

## 6. Answering

Retrieval returns candidates. Turning those into a sentence is a separate operation
with a separate failure mode: **a list is checked, a sentence is believed.**

The context builder enforces three rules in order:

1. **Drop duplicates.** Overlapping chunks would otherwise be paid for twice and
   make one passage look corroborated when it is a repeat.
2. **Two sources per document.** More than that crowds out other files.
3. **A token budget, not a character count.** Characters do not bound tokens; the
   measured ratio on this corpus is 3.4 characters per token.

The model is told to cite `[n]` for every sentence. The response is then checked:

- a cited number that was not sent means the model is not grounding its output, and
  the whole answer is flagged
- an answer with no citations is flagged as an assertion rather than a result
- "I could not find that" is a valid and expected outcome

---

## 7. Measured, not assumed

The numbers below are from runs on one machine and are recorded in `CHANGELOG.md`
and the commit messages. They are the part worth bringing to an interview, because
each one changed a decision.

| Question | Measurement | Decision |
|---|---|---|
| Is 1000-character chunking safe? | 8 of 5,726 chunks exceeded the model's 512-token window | chunk by tokens, with offsets |
| Is OCR fast enough? | 2.0–7.5 s per image | opt-in, with a cost estimate |
| Does SigLIP find pictures by description? | 3/4 on images with known answers | shipped, with its limits documented |
| Does a cross-encoder reranker help? | MRR 0.5350 → 0.5333, 937 ms per query | **not shipped** |
| Is the trigram index worth it? | 11.1 GB, one query type | optional, off by default |
| How fast is Whisper? | 0.6 s for 8.8 s of audio (14× realtime) | opt-in per recording |
| How fast is video sampling? | 2.58 s per frame by decoding all → 0.19 s by seeking | seek to sample points |

**The reranker is the one to lead with.** It was built, wired, measured against a
benchmark with known answers, and **rejected** — because 937 ms per query bought
nothing. Shipping it would have been easy and defensible, and wrong.

---

## 8. Failure modes that were found by running it

Each of these produced plausible output and no error:

- **A PDF reported one page.** `pdftext` joins pages with no separator, so
  splitting on `\f` produced a single "page". Content was complete; the boundaries
  were gone. Every citation on a 103-page file said page 1.
- **Semantic search was off for a bookkeeping reason.** The vector manifest had been
  quarantined by a schema change, and a bare `except` reported it as corruption. The
  vectors were correct and unread.
- **The trigram index had never been queried.** It was built, filled, marked ready,
  and every query went to the slow path because the query hit the wrong database.
- **`0 pictures, 0 video, 0 audio`** on a machine with 12,554 images, because the
  SQL that extracted extensions computed the wrong position.
- **Buttons with no CSS at all** rendered as browser-default grey, which reads as
  disabled.

The common shape: **the failure returns results, just fewer or slower or from the
wrong place.** A suspiciously round number is usually the tell.

---

## 9. What is deliberately not here

| | why |
|---|---|
| HNSW / IVF-PQ | wrong side of the size threshold; a full scan is faster here |
| A vector database | operational cost with no benefit at this scale |
| Cloud anything | the premise is that nothing leaves the machine |
| Reranking | measured, no gain (see §7) |
| Cross-platform builds | the scripts exist; they have not been run |

---

## 10. Explaining it in five minutes

1. **The problem**: three things a filename search cannot do — fragments, meaning,
   media.
2. **The shape**: classify the query, run the backends that can answer it, fuse by
   rank, build context within a token budget, answer with citations.
3. **The evidence**: the reranker that was rejected, the PDF that reported one page,
   the index that was never queried. Each is a measurement that changed a decision.
4. **The constraint**: everything local, no cloud, no account.
5. **The honest limits**: semantic covers 4,488 of 865,519 documents; image search
   finds pictures rather than counting objects; the build is unsigned.
