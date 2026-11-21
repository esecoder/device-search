# Bridge the gaps — every step, in order

Each step is sized, states what it unlocks for a real user, and says how it will be
verified. Verification means running it against real files, not reading the code.

Order is deliberate: **things a user hits in their first five minutes come first**,
and distribution comes last because nothing is worth shipping until the search is
complete.

---

## Phase 1 — Make the search complete (the user's first session)

### 1. Images: OCR (text inside screenshots) — 1,295 files, currently unsearchable

**Why first:** a screenshot is the single most common thing a person cannot search.
They have the information, they cannot find it, and no other tool solves it.

**Steps**
1. Install `rapidocr-onnxruntime` — needs `opencv`, which must come from a wheel, not a source build.
2. Extract on ingest: image → text → `method="ocr"`, stored like any other document.
3. Add OCR to the incremental crawl so images are not skipped as unchanged.
4. Record `ocr_unavailable` counts in `ds stats` so a missing engine is visible.
5. Verify: search for text visible in a real screenshot.

**Done when:** `ds search "<words in a screenshot>"` returns the image, and `ds stats` reports how many images were read and how many failed.

### 2. Images: what the image *shows*, not just what it says — 2-3 days

**Why:** "the screenshot of the pricing table" cannot be answered by OCR if the
table is a picture of numbers. This needs a vision model.

**Steps**
1. Caption each image with a vision model through the existing provider layer (local Ollama `llava`, or a configured API).
2. Store the caption as searchable text alongside the OCR text, clearly labelled.
3. Make it opt-in, because it costs a model call per image.
4. Show progress and cost before starting, like the code index.
5. Verify: search for a concept that appears only visually.

**Done when:** images are findable by description, and the user was told what it cost.

### 3. Folder selection with live cost — 3-4 hours

**Why:** the difference between a 300 MB index and a 19 GB one. This is the step
that stops the app being unusable by accident.

**Steps**
1. Folder tree in Settings with checkboxes, defaulting to the exclusion list.
2. Each node shows: files, estimated index size, estimated time.
3. Totals update as boxes are ticked.
4. Selecting a folder that contains excluded subfolders shows *why* they are excluded, with an override.
5. Apply → crawl only the selected roots; unselected roots are removed from the index.
6. Verify: tick a folder, see the number change, index it, confirm only it was indexed.

**Done when:** a user can see the cost of a choice before making it, and the numbers match what happens.

### 4. Citations: say where the answer came from — 1 day

**Why:** an answer without a source cannot be trusted, and every serious RAG product
shows one.

**Steps**
1. Carry the source document and chunk through generation.
2. PDFs already store `[page N]` markers — surface the page number.
3. Render sources under the answer as clickable rows that open the file.
4. Verify: ask a question answerable only by a PDF; check the page number is right.

**Done when:** every generated sentence can be traced to a file and a location.

---

## Phase 2 — Make the answers good (the second session)

### 5. Context engineering — 2 days

**Why:** retrieval quality is capped by what fits in the prompt, and today the
retrieved chunks are simply concatenated.

**Steps**
1. A token budget per request, computed from the model's window.
2. Deduplicate near-identical chunks before they reach the prompt.
3. Prefer diverse sources over many chunks from one file.
4. Refuse to answer when the retrieved context does not contain the answer, instead of padding.
5. Verify: measure answer quality with and without, on a fixed question set.

**Done when:** a measured improvement on a held-out question set, not an assertion.

### 6. Multi-hop retrieval — 3 days

**Why:** "what did I decide about the database" needs to find a decision, then the
document it refers to. One pass cannot.

**Steps**
1. After the first retrieval, ask whether the question is answered.
2. If not, extract the missing entity and search again.
3. Cap at N hops, with a visible trace of what was searched.
4. Verify: a benchmark of questions that provably need two lookups.

**Done when:** multi-hop questions succeed where single-pass fails, measured.

### 7. Reranking — 1 day

**Why:** retrieval finds candidates; ordering them is a separate, cheaper problem
than finding them.

**Steps**
1. Score top-N candidates with a cross-encoder, or the configured model.
2. Keep it optional and measured — a reranker that hurts must be visible as such.
3. Verify: MRR and nDCG before and after, with a check that rejects it if it makes things worse.

**Done when:** the metrics improve, or the reranker is off.

---

## Phase 3 — Make it complete (media beyond images)

### 8. Audio: transcription then search — 3-4 days

**Steps**
1. Transcribe with a local model (`faster-whisper`), storing timestamps.
2. Index the transcript as text, with the timestamp attached to each segment.
3. Surface the timestamp in results so a hit plays from the right place.
4. Verify: find a phrase spoken in a real recording, and confirm the timestamp.

### 9. Video: frames plus transcript — 3-4 days

**Steps**
1. Extract the audio track and transcribe it (step 8).
2. Sample frames at scene changes, caption them (step 2).
3. Index both, keyed to timestamps.
4. Verify: find a moment by what is said, and by what is shown.

---

## Phase 4 — Distribution

### 10. Installer and signing — 2 days

**Steps**
1. `.dmg` with drag-to-Applications.
2. Apple Developer account, code sign, notarise — without this macOS refuses to open it.
3. Windows and Linux builds on their own runners.
4. Verify: install on a machine that has never had this repository.

### 11. First-run experience — 1 day

**Steps**
1. Choose folders, see cost, start indexing, see progress.
2. Explain what is private and what is not, once, plainly.
3. Verify: someone who has never seen it gets a search result without help.

---

## What is deliberately not in this list

| | why |
|---|---|
| Mobile app | its own architecture; a separate project |
| Database connectors | needs credentials and a different security model |
| Cloud sync | contradicts "never leaves the machine" |
| A vector database | at these sizes, sharded `.npy` files are faster and simpler; revisit past ~5M chunks |

---

## The order, in one line

**OCR → folder cost UI → citations → context engineering → multi-hop → rerank →
audio → video → installer → first-run.**

Every one is verified by running it against real files before it is called done.
