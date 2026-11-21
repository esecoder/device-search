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

Measured, not intended.

### Working

| | |
|---|---|
| Exact fragment search | ✅ `InputLayer(shape=(784,))` works |
| Keyword search (FTS5/BM25) | ✅ 0.002 s at 852,000 documents |
| Filename and path search | ✅ 0.45 s |
| Semantic search | ✅ working, **but over 13,336 vectors of 852,000 documents** |
| PDF text extraction | ✅ 361 PDFs indexed and searchable |
| Ask a question | ✅ AI panel, grounded, with sources |
| Metadata queries | ✅ size, extension, date |
| Query routing | ✅ classifier picks backends, broadens on failure |
| Desktop app | ✅ Tauri, tray icon, global hotkey |
| Resumable vector build | ✅ sharded, crash-safe |
| Cost shown before expensive actions | ✅ code index, folder scope |

### Not built

| | impact |
|---|---|
| **Image understanding (OCR)** | 1,295 images are unsearchable — the engine is not installed |
| **Image captioning** | cannot answer "a screenshot of the pricing table" |
| **Video understanding** | not started |
| **Audio transcription** | not started |
| **Multi-hop agentic retrieval** | single-pass only; no genuine multi-step search |
| **Context engineering policy** | retrieval is concatenated; no budget or dedupe policy |
| **Citation back to page/timestamp** | PDFs store page markers but the UI does not surface them |
| **Sub-folder selection with live cost** | all-or-nothing per root |
| **Packaging** | the `.app` imports Python from this repo path; it cannot be moved or shared |

### The gap in one line

**Text search is production quality. Multimodal is absent. The agent is one
pass. And the binary cannot leave this machine.**

---

## What "done" looks like

A person who has never seen this repository can:

1. download a build for their platform,
2. pick the folders they want, seeing the time and disk each will cost,
3. start it, and get an answer to a question about their own files,
4. search by text, meaning, image, and audio,
5. and never have anything leave their machine unless they chose to connect a model.

That is the target. Everything else is a step toward it.
