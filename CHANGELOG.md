# Changelog

Releases are `MAJOR.MINOR.PATCH`, tagged `vX.Y.Z`, and the number lives in
`VERSION`. `./bin/release.sh` keeps it in step with `__init__.py`, `tauri.conf.json`
and the bundle's `Info.plist`, then builds, verifies and tags.

**Why this file exists at all:** the first nine installers were named after commit
hashes and all reported version `0.1.0`. There was no way to tell which was newer,
which contained a fix, or which one someone had. A version that changes is the
only thing that makes "is my copy current?" answerable.

---

## 0.1.0

First release. Everything below is in the box.

### Search

- **Exact fragments** — `InputLayer(shape=(784,))` resolves to the file and line.
- **Keyword** — FTS5/BM25, 2 ms at 865,000 documents.
- **Filename and path** — 0.45 s.
- **Meaning** — local embeddings via ONNX, no network.
- **Images by description** — SigLIP2, 768 dimensions.
- **Video frames** — sampled every 10 s, a hit carries its timestamp.
- **Audio** — Whisper transcription, opt-in, a hit carries the moment it was said.
- **Metadata** — size, extension, date, with "I did not look" distinguished from
  "nothing matched".

### Answers

- Grounded answers from your files, with **citations that carry a page number** for
  PDFs and a timestamp for audio and video.
- Invented citation numbers are detected and the answer is flagged.
- **Context engineering**: a 6,000-token budget, duplicate removal, two sources per
  document.
- **Multi-hop**: follows a reference from a result into the document it names.
- Providers: local Ollama and LM Studio, OpenAI, DeepSeek, Anthropic, OpenRouter,
  or any OpenAI-compatible endpoint.

### Data

- PDF text with page boundaries, OCR for screenshots, PDF/OCR failure counts in
  `ds stats`.
- 51 directories excluded by default, including toolchains, build output and AI
  agent working directories.
- **Every expensive action shows its cost first** and waits for a decision: folder
  scope, the code index, the image index, transcription.
- Resumable sharded vector storage with per-shard fingerprints.

### Desktop

- Menu-bar icon, global shortcut `⌃⌥Space`, floating panel.
- Closing the window hides it; it does not quit.
- Rounded, opaque window.
- Settings: folders with live file counts and sizes, model, code index, media.

### Packaging

- **85 MB self-contained `.dmg`** — runs on a machine with no Python and no
  repository.
- Unsigned. First launch needs right-click → Open. Signing needs an Apple Developer
  account; the commands are in the README.

### Known limitations, stated rather than discovered later

- **Meaning-search covers 4,488 of 865,519 documents.** The rest are findable by
  exact, keyword and filename search, which is unaffected.
- **Reranking was measured and rejected.** 937 ms per query for no gain on this
  corpus (MRR 0.5350 → 0.5333). It stays off.
- **Image search finds pictures, it does not count objects or reason about
  position.** "Three cars" and "left of" are beyond SigLIP at 224×224.
- **Video is found by frame, not by motion.**
- **OCR reads text that is printed; it cannot describe a photo that has none.**
- **Windows and Linux builds exist as scripts and have not been run on either.**
