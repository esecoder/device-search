# AI Stack

What this project actually covers, area by area — with the file that demonstrates
each one, so a claim can be checked rather than taken on trust.

Written for two readers: someone deciding whether to hire on the strength of this
work, and the author, who has to explain it without notes.

**Size:** ~15,100 lines — 9,393 Python, 2,865 UI (JavaScript/CSS/HTML), 526 Rust,
2,319 tests.

---

## At a glance

| Area | Depth | Where |
|---|---|---|
| Data engineering and curation | **deep** — the largest part | `crawl.py`, `extract.py`, `config.py` |
| Retrieval / RAG | **deep** | `store.py`, `semantic.py`, `agent.py`, `multi_hop.py` |
| Multimodal inference | **deep** | `image_search.py`, `video.py`, `audio.py` |
| LLM application | **moderate** | `answer.py`, `llm.py` |
| Evaluation | **deep** — six harnesses | `test/measure_*.py` |
| Systems and reliability | **deep** | `vectors.py`, `server.py`, `runtime.py` |
| Product and UX | **moderate** | `ui/` |
| Packaging and release | **deep** | `bin/` |
| **Model training** | **none** | — |

---

## 1. Data engineering and curation

The area most AI engineers are weakest in, and the largest here. Everything the
models do depends on what survived this stage.

**Format dispatch by cost.** The extractor picks the cheapest tool that can read a
file: a UTF-8 read is microseconds, `pdftext` is milliseconds, OCR is seconds per
image, Whisper is seconds per second of audio. Expensive extractors are opt-in and
show an estimate first.

**Extraction provenance.** Every document records how its text was obtained —
`utf8`, `pdftext`, `ocr`, `whisper`. "Found in scan.pdf" and "found in scan.pdf via
OCR" are different claims about reliability, and a user checking a hit needs to know
which one they are relying on.

**Scope control, justified by measurement.** 51 directories excluded by default.
Not arbitrary: a home-directory index was 88% toolchains and caches — 755,412 of
859,569 documents were `.git` internals, `.npm` packages and build output. Excluding
them left 78,915 documents.

**Two size ceilings.** 2 MB for text, 2 GB for media. One limit cannot serve both:
a one-minute screen recording is 1.5 MB, and the text limit rejected 79 media files
before the extractor ever saw them.

**Failure accounting.** `pdf_unavailable`, `audio_skipped`, `audio_silent`,
`binary` are counted states in `ds stats`, not silent drops. An empty document is a
normal thing to store, which is exactly how 255 unreadable PDFs went unnoticed.

**The principle:** *"I found nothing" and "I did not look" are different answers,
and only the backend knows which one it gave.*

---

## 2. Retrieval and RAG

**Word index — FTS5.** An external-content table (`content='documents'`) with
INSERT/UPDATE/DELETE triggers, so the index maintains itself and the text is not
stored twice. **2 ms at 865,000 documents**, against 351 seconds for the Python BM25
fallback it replaced. Both use the same `k1`/`b` so they agree rather than quietly
diverging.

**Dense retrieval — `bge-small-en-v1.5`.** 384 dimensions via ONNX Runtime, no
PyTorch on the default path. Normalised at build time so search is one dot product.

**Query classification.** A rule-based router reads the query shape: punctuation and
no spaces means a fragment (exact), a question mark or interrogative opening means
semantic, a filename pattern means path. **A fixed pipeline is not an agent** — the
classifier is what makes the routing agentic, and a miss triggers one broadening
pass over the backends it skipped.

**Rank fusion — Reciprocal Rank Fusion.** `score = Σ 1/(k + rank)` with k=60. It
needs no score normalisation, which matters because BM25 is unbounded (typically
5–40) and cosine similarity sits around 0.1–0.2. Adding those directly would put
every keyword hit above every semantic one.

**Multi-hop retrieval.** Follows a path-shaped reference found *inside* a result
into the document it names, then searches again. The reference must already exist in
the index, so a hop is a lookup rather than a guess — the worst case is that nothing
is added, and a wrong hop is impossible.

**Cross-encoder reranking — built, measured, rejected.** `ms-marco` / `jina-reranker`
scores query and document in one forward pass. On this corpus it moved MRR from
0.5350 to 0.5333 and cost 937 ms per query against 65 ms for retrieval. **It is
off.** Shipping it would have been easy, defensible, and wrong.

---

## 3. Multimodal inference

**Images by description — SigLIP2.** An image and a sentence embedded into the same
768-dimensional space, so "a red car parked near a house" can be compared against a
photograph. Verified on images with known answers: **3 of 4 matched their own
description**, against a chance rate of 25%. The miss was a synthetic "page of text"
drawn as grey bars, which genuinely resembles a bar chart.

**Text inside images — RapidOCR.** 2.0–7.5 seconds per image, opt-in.

**PDF with page boundaries.** `paginated_plain_text_output`, because
`plain_text_output` joins every page with **no separator at all** — splitting on
`\f` produced a single "page" holding the entire file, so every citation on a
103-page document said page 1.

**Audio — Whisper via faster-whisper and CTranslate2.** Torch-free, **14× realtime**
(0.6 s for 8.8 s of audio). Text is stored with `[M:SS]` markers so a hit can point
at the moment something was said.

**Video — frame sampling with seeking.** Adjacent frames at 30 fps are identical for
search purposes, so frames are sampled at a fixed interval. Decoding all 5,670
frames to keep 19 cost **2.58 s per kept frame**; seeking to the sample points costs
**0.19 s** — 13× faster, because the cost was the decode, not the model.

**All three return a position, not just a file.** A citation into a document with no
location is not a citation.

---

## 4. LLM application

**Grounding.** Answers are built only from retrieved sources, and refusal is a
first-class outcome rather than an error.

**Citation validation.** The model is asked to cite `[n]`. Any cited number outside
the set that was sent flags the **whole answer** as ungrounded — a sentence that
cites a source that does not exist is evidence the model is not grounding its output,
not a formatting slip. An answer with no citations is marked as an assertion.

**Context engineering.** Three rules in order:

1. **Drop duplicates** — overlapping chunks would otherwise be paid for twice and
   make one passage look corroborated when it is a repeat.
2. **Two sources per document** — more crowds out other files.
3. **A token budget, not a character count** — 6,000 total, 1,200 per snippet. The
   per-snippet ceiling was found by testing: without it, one 20,000-character
   document consumed **5,883 of 6,000 tokens** and nine retrieved files were never
   sent.

**Provider abstraction.** One module speaks both OpenAI's `/chat/completions` and
Anthropic's `/v1/messages` — different endpoint, different auth header, different
response shape. Offering a provider and sending it the wrong body fails with a 400
that reads like a bad key.

---

## 5. Evaluation

**Six harnesses**, and this is the area worth emphasising most.

| Harness | Question |
|---|---|
| `measure_embedders.py` | which embedding model, at what cost |
| `measure_fusion.py` | does RRF beat concatenation |
| `measure_recall.py` | what is the retrieval ceiling |
| `measure_rerank.py` | does reranking help at all |
| `measure_policy.py` | which chunking policy |
| `rerank_bench.py` | end-to-end, on a self-built query set |

**Metrics written from scratch:** MRR, nDCG@k, recall@k, Wilson score intervals,
exact McNemar for paired comparison.

**Ground truth is constructed, not judged.** Queries are built from a document's
distinctive terms, and the document they came from is the correct answer. That makes
grading automatic and impossible to mark generously.

**And the test set is validated before it is used.** The first reranker benchmark
said "MAKES RESULTS WORSE" — and checking why found that **6 of 12 sampled documents
could not be retrieved by their own terms**, because the sample was full of minified
JavaScript. A benchmark whose ground truth is unreachable measures retrieval failure
and blames the reranker for it. Excluding those documents moved the baseline from
0.1375 to 0.5350 and changed the verdict to "no measurable difference".

**Negative controls throughout:** every detector is validated against input whose
answer is already known. A check that cannot fail is not a check.

---

## 6. Systems and reliability

**Sharded, resumable vector storage.** One `.npy` per ~200 documents plus a
manifest recording which documents each shard covers, with a content fingerprint per
shard. A build killed at 650,000 of 859,569 continues from there.

**Crash safety by ordering.** Data files are written and flushed before the manifest
that references them, so a crash leaves the two disagreeing in the recoverable
direction rather than the dangerous one.

**Process liveness, including zombies.** `os.kill(pid, 0)` answers "does this pid
exist", and a `<defunct>` process exists. A finished crawl left one behind and the
app refused to index for as long as it lasted. The check reads the process state and
treats `Z` as dead.

**A local HTTP daemon** with token authentication, a Host-header check (DNS
rebinding), and CORS — including the `OPTIONS` preflight that a custom header forces.

**Runtime probing by import.** ONNX Runtime 1.23.2 fails to load on macOS 13 with a
missing symbol. Detecting that by `find_spec` finds the package and reports success;
importing it reports the truth.

---

## 7. Product and UX

**Cost shown before every expensive action.** Folder scope, the code index, the
image index and transcription all print time and disk and wait for a decision.

**Two-tier results.** `MATCHES` (your words are in these files) and
`CLOSEST BY MEANING` (no word match, leads rather than findings). A single merged
list would imply a ranking that does not exist.

**Honest emptiness.** "No file matches 10 GB" was a claim about the disk made from
an index that holds nothing above 2 MB. It now says what was searched, why the
answer is not a real "no", and what to do about it.

**Progress with ETA.** Every long operation reports what it has done and how long is
left, because "12% done, 8 minutes left" tells a user whether to wait; a spinner
does not.

---

## 8. Packaging and release

**Self-contained binary.** PyInstaller freezes the daemon — interpreter, packages
and code — into 48 MB. Verified by copying the bundle to `/tmp` and running it with
no repository on the path.

**Desktop shell.** Tauri 2 with a menu-bar icon, a global shortcut, and a frameless
floating panel. The menu-bar icon is black plus alpha, because macOS masks template
images and discards colour.

**Version management.** `VERSION` is the single source, synced into `__init__.py`,
`tauri.conf.json` and the bundle's `Info.plist`, with a test that fails when they
disagree. The release script refuses to run on a dirty tree or when any source file
is newer than the built app.

**Publishing.** GitHub Releases with SHA-256 checksums, using `gh` when present and
the REST API otherwise, idempotent so a retry replaces rather than duplicates.

---

# What this project does not cover

**It is an inference and data-engineering project, not a training one.**

| | |
|---|---|
| Model pretraining | not attempted |
| Fine-tuning (LoRA, QLoRA, SFT) | not attempted |
| RLHF / DPO / GRPO | not attempted |
| Distributed training (DDP, FSDP, TP, PP) | not attempted |
| Model architecture work | not attempted |

If the question is "has this person trained a model", the honest answer is no, and
this project is the wrong evidence. What it does demonstrate is the work that
surrounds a model in production: getting data in, getting the right data out,
measuring whether any of it helps, and shipping it.

---

# The five-minute version

1. **The problem.** Three things a filename search cannot do: fragments
   (`InputLayer(shape=(784,))`), meaning ("how do I configure the embedding
   dimension"), and media (a screenshot, a recording).
2. **The shape.** Classify the query, run the backends that can answer it, fuse by
   rank, build context within a token budget, answer with citations that carry a
   page or a timestamp.
3. **The evidence.** The reranker that was rejected, the PDF that reported one page,
   the index that was never queried, the count that said zero on a machine with
   12,554 images. Each is a measurement that changed a decision.
4. **The constraints.** Everything local. No cloud, no account, no upload unless the
   user connects a model and chooses to send something.
5. **The limits.** Meaning-search covers 4,488 of 865,519 documents. Image search
   finds pictures rather than counting objects. The build is unsigned.

---

# One line

> A production-shaped RAG system over 865,000 local files, with four retrieval
> backends, five multimodal extractors, and a measured refusal to ship a reranker
> that did not earn its latency.
