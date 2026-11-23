# device-search — what this is meant to be

This is the reference for every scope decision. When something is unclear, the
answer is here, not in a commit message.

---

## The one-sentence goal

**Find anything on your device, by any means that works — text, meaning, images,
video, audio — with an AI that answers questions about it, in an interface that
opens as fast as Spotlight and never leaves the machine.**

---

## What "anything" means

A user should be able to type any of these and get a useful answer:

| Query | What has to happen |
|---|---|
| `InputLayer(shape=(784,))` | exact substring, across every file type |
| `retrievers.py` | filename / path matching |
| `bm25 reranker` | keyword relevance |
| `where do I configure the embedding dimension` | semantic retrieval over meaning |
| `a screenshot of the pricing table` | image understanding (OCR + captioning) |
| `the video where the chart spikes` | video understanding |
| `the meeting where we discussed pricing` | audio transcription + retrieval |
| `files over 2 GB from last week` | metadata queries |
| `how does our auth work` | multi-step retrieval + synthesis, not one lookup |
| `what did I decide about the database` | answer from across several documents |

Every one of those is a different retrieval problem. **A single embedding index
cannot answer all of them, which is why the tool routes the query to the right
backends rather than running one.**

---

## The five things this must demonstrate

This project exists partly as a working demonstration of modern AI engineering.
Each item below is a capability that has to be real, not described:

### 1. A full RAG pipeline

- ingestion and extraction across file types
- chunking that respects the model's token window
- hybrid retrieval: lexical (BM25/FTS5) + dense (embeddings)
- reranking
- grounded generation with citations back to the source
- abstention when the corpus does not contain the answer

### 2. Modern agentic search

- a query classifier that decides which tools to run
- tool use with budgets (steps, calls, time)
- observation and broadening when the first pass fails
- multi-hop retrieval: an answer that requires two or three lookups
- a trajectory that can be inspected, not just a final answer

### 3. Context engineering

- a token budget per request, and a deliberate policy for filling it
- deduplication and diversity in what gets retrieved
- recognising when retrieved context is insufficient rather than padding it
- the difference between "I could not find it" and "I did not look"

### 4. Multimodal understanding

- **images**: OCR for text in screenshots, and understanding of what the image is
- **video**: sampled frames plus transcripts, retrievable by what happens in them
- **audio**: transcription, then retrieval over what was said
- **PDFs**: text with page positions, so a hit can say which page

### 5. Excellent UX

- opens instantly from a hotkey or tray icon
- answers while it is still searching
- says when the index is incomplete rather than implying completeness
- states the cost of every expensive action before doing it
- never silently fails: an error stays on screen until it is addressed

---

## Non-negotiable constraints

| | |
|---|---|
| **Private by default** | nothing leaves the machine unless the user connects a model and chooses to send it |
| **Cross-platform** | macOS, Windows, Linux. No OS-specific API as a foundation |
| **Local models supported** | Ollama, or any OpenAI-compatible endpoint, or none at all |
| **No browser dependency** | a real desktop window, not a wrapped web page |
| **Open source** | anyone can run it, read it, and change it |
| **Torch is optional** | the base install is ONNX-based and small |

---

## Honest state, as of this document

Measured on this machine, not intended. Numbers are from actual runs.

### Working

| | |
|---|---|
| Exact fragment search | `InputLayer(shape=(784,))` — 0.060 s with the optional code index, ~9 s without |
| Keyword search (FTS5/BM25) | 0.002 s at 852,000 documents |
| Filename and path search | 0.45 s |
| Semantic search (text) | working — 13,336 vectors, 4,488 documents |
| **Image text (OCR)** | **12,554 images** read; searching inside a screenshot returns it in 83 ms |
| **Image description (SigLIP2)** | **1,210 images** embedded, 768 dims, 4 MB; *"a red car near a house"* verified 3/4 on known answers |
| **PDF text** | 386 PDFs, with page numbers — exact on a 39-page document |
| **Audio** | **44 files**, opt-in; Whisper at 14x realtime, timestamps locate the spoken word |
| **Video** | **131 files, 1,458 frames**, opt-in; a hit says *"2:50"* |
| Ask a question | grounded, with citations and page/timestamp |
| **Context engineering** | 6,000-token budget, duplicate removal, 2-per-document quota |
| **Multi-hop retrieval** | follows a reference from a result into the file it names |
| **Reranking** | measured and **rejected**: no gain, 14x slower |
| Metadata queries | size, extension, date — including *"I did not look"* vs *"nothing matched"* |
| Query routing | classifier picks backends, broadens when the first pass is empty |
| Desktop app | Tauri, tray icon, global hotkey |
| Resumable vector build | sharded, crash-safe, fingerprints per shard |
| Cost shown before expensive actions | code index, **image index, media transcription**, folder scope |
| **Packaging** | **85 MB .dmg** that runs on a machine with no Python and no repository |

### Not built

| | impact |
|---|---|
| **Image captioning** | OCR reads text; it cannot describe a picture that has none. SigLIP narrows the gap and does not close it — no counting, no spatial relations |
| **Video motion** | frames are still images; "the part where it scrolls" is not findable |
| **Semantic coverage** | vectors cover 4,488 of 852,000 documents — 0.5% |
| **Code signing** | unsigned, so macOS shows "unidentified developer". Needs a paid Apple account |
| **Windows and Linux builds** | the scripts exist; nobody has run them on those platforms |
| **Mobile** | not started, and not started on purpose |
| **Database connectors** | not started; needs a different security model |

### The gap in one line

**Every modality in the vision is now searchable, and two of them are only half
understood: images are found by description and not counted, video is found by
frame and not by motion. Signing is the only thing between this and a stranger
installing it without a warning dialog.**

### What changed since the first draft of this section

The first version said: *"Text search is production quality. Multimodal is
absent. The agent is one pass. And the binary cannot leave this machine."*

Every clause of that is now false, and each one took a measurement to disprove:

- multimodal was absent because **the libraries were never installed**, not
  because the code was missing
- the agent was one pass because **nobody had written the second one**
- the binary could not leave because **the bundle contained no Python**

All three were invisible from the source. They were visible from running it.

## What "done" looks like

A person who has never seen this repository can:

1. download a build for their platform,
2. pick the folders they want, seeing the time and disk each will cost,
3. start it, and get an answer to a question about their own files,
4. search by text, meaning, image, and audio,
5. and never have anything leave their machine unless they chose to connect a model.

That is the target. Everything else is a step toward it.
