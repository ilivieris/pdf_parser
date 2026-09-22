# Document Processor Service

Minimal document-processing API for the chatbot template.

## Endpoints

- `GET /health`
- `POST /extract`

## Contract

- `/extract` loads a file from local disk using `path`. `path` may be an absolute filesystem path, or a path relative to `DOCUMENT_OUTPUT_ROOT`. The file's extension (from `path` itself) picks the parser — there is no separate `filename` override.
- When running via Docker Compose, `path` is resolved **inside the container**, so the source file must be reachable through a mounted volume (see `docker-compose.yml`).
- Supported extraction formats are `PDF`, `DOCX`, `DOC`, and text-like files listed in `core/config.py`.
- `post_processing` controls what gets written back to disk, as a single flat file directly under `DOCUMENT_OUTPUT_ROOT` — no subfolders. The file is named after `artifact_id` (a content hash), not the source filename:
  - `none` (default): the raw extracted text, as-is, written to `{DOCUMENT_OUTPUT_ROOT}/{artifact_id}.txt`.
  - `clean`: the text is sent to the configured OpenAI model to fix spelling/grammar and improve layout, written to `{DOCUMENT_OUTPUT_ROOT}/{artifact_id}_clean.txt`.
  - `markdown`: the text is sent to the configured OpenAI model with its own prompt that fixes spelling/grammar **and** formats the result as markdown in a single pass (an independent correction call, not `clean` followed by a markdown conversion step), written to `{DOCUMENT_OUTPUT_ROOT}/{artifact_id}.md`.
- If a `clean`/`markdown` request's text is too large for the configured OpenAI model (see "Large documents" below) or the correction call fails, the raw extracted text is written instead and the response's `note` field explains why.
- Extraction failures return an error instead of silently writing empty output.

## Large Documents And Chunking

The per-chunk budget is `DOCUMENT_CHUNK_TOKENS` (default `8000`), deliberately far below the model's output cap — faithful reproduction degrades long before that cap is reached:

- If the document fits in one chunk, it's corrected in a single OpenAI request.
- Otherwise it's split into paragraph-aligned chunks of at most that size, corrected in parallel (`DOCUMENT_CORRECTION_CHUNK_PARALLELISM` concurrent requests), and the corrected chunks are joined back together in order. One request per chunk — no retries.
- If the document is **over `10 × MAX_TOKENS`**, correction is skipped entirely and `note` in the response explains that it was too large.
- If an OpenAI call fails, the original extracted text is written instead and `note` explains why.

### How `DOCUMENT_CHUNK_TOKENS=8000` Was Chosen

The value is measured, not guessed. A 168,849-token Diavgeia document was sliced at four offsets and each slice corrected with the production prompt in a single call, scoring the output by character ratio and by per-decile word alignment against the input (an omitted passage shows up as a decile collapsing to zero):

| Chunk size | Regions tested | Result |
| ---: | ---: | --- |
| 2,000 – 12,000 | 16 | complete in every one |
| 14,000 | 3 | one region stopped at ~30% of the input |
| 16,000 | 4 | one region stopped at ~30% of the input |
| 24,000 | 2 | one region stopped at ~20% of the input |

Two things matter about the failures. They are **stochastic**, not size-deterministic — 16,000 passed the same region that 14,000 failed — so the fix is margin, not a threshold. And they are **silent**: `finish_reason` is still `stop`, `completion_tokens` is nowhere near `MAX_TOKENS`, and no placeholder text is emitted. Nothing in the response flags it.

Hence 8,000: a 1.75× margin below the smallest size ever observed to fail. On the 168,849-token document that is **27 chunks in 3 parallel waves** (2,000 would be 96 chunks in 10 waves, for the same total token spend).

If you change the model, re-run the measurement — this threshold is a property of the model, not of the code.

### Why Not A Cheaper Model

`gpt-4.1-mini` was measured with the identical method (20 runs: 5 sizes × 4 offsets). It is **not** usable for this task, and no `DOCUMENT_CHUNK_TOKENS` value fixes it, because its failures are not size-correlated:

| Size | Complete | Failure observed |
| ---: | ---: | --- |
| 2,000 | 3/4 | dropped ~45% of one slice |
| 4,000 | 4/4 | — |
| 8,000 | 3/4 | **fabrication**: looped until the output cap |
| 12,000 | 3/4 | duplication: 27% of output was repeated passages |
| 16,000 | 3/4 | duplication: 37% of output was repeated passages |

The 8,000 case is the disqualifying one. From a 20,053-character input whose highest paragraph reference is `παρ. 9`, the model emitted 67,289 characters containing 2,294 paragraph references counting up to `παρ. 2252` — none of which exist in the source. It invented over two thousand legal citations and only stopped because it hit `MAX_TOKENS`.

`gpt-4.1` on the same slices: 0.1% repeated shingles in, 0.1% out, and no fabricated references in any of the 20 runs. Its only failure mode is dropping content at ≥14,000 tokens, which `DOCUMENT_CHUNK_TOKENS=8000` avoids.

For a document delivered to a ministry, a model that invents citations is categorically worse than one that occasionally truncates. Keep `OPENAI_MODEL=gpt-4.1`.

## Historical Comparison

`docling` has been retired from this repository. The active document-processing flow is the generic processor in `document_processor_service`.

This section keeps the side-by-side results that were generated on **2026-07-01** so they remain visible directly in the README.

### Source Document

- File: `21-0146-02_Mousiki-G-Gymnasiou_Tetradio-Ergasion.pdf`
- Dataset: `tools/data`

### Runs Captured

- Generic processor batch run: `2026-07-01T17:55:06Z`
- Docling comparison run: `2026-07-01T18:13:37Z`

### Summary Table

| Metric | Generic processor | Docling |
| --- | ---: | ---: |
| Characters extracted | 59,322 | 68,567 |
| Tokens | 8,946 | 7,470 |
| Greek alpha ratio | 0.8082 | 0.9214 |
| Greek token ratio | 0.7676 | 0.9051 |
| Noisy token ratio | 0.0000 | 0.0234 |
| Non-empty lines | 476 | 935 |
| Latin-dominant line ratio | 0.2647 | 0.0920 |
| Chunk evaluation score | 100.0 | n/a |
| Markdown evaluation score | 100.0 | n/a |
| Triplet validation violations | 0 | n/a |
| Measured runtime | not recorded in artifact | 92.135 s |

### Notes

- The generic processor is the production path that remains enabled in the codebase.
- The generic processor produced a fully evaluated batch output with `excellent` chunk and markdown verdicts, plus `0` triplet-validation violations.
- Docling extracted more raw text volume and showed a higher Greek-language ratio, but it also introduced a non-zero noisy-token ratio and a much more fragmented line structure.
- The encoding previews stored in the original comparison artifacts suggest that both runs still contained OCR/PDF noise, with docling preserving more line-level fragmentation.

### Conclusion

The repository now keeps the generic processor only. The historical `docling` comparison remains documented here for review, while the transient run artifacts are no longer needed.

## Final Verification

Test file:

- `tools/data/Σημειώσεις σε Λειτουργικά Συστήματα - θέματα.doc`

Result from the current minimal pipeline:

- Extract: `char_count=212396`
- Extract text preview starts with the table of contents in Greek as expected
- Markdown export: `extracted/790ff7612eeff714.md`

Docling status in this runtime:

- `docling` is not installed in the current container, so a fresh docling comparison could not be generated here
- `importlib.util.find_spec("docling") -> None`

If docling is reintroduced later, this section is the place to add a direct side-by-side comparison for the same test file.

## Run With Docker Compose

From the project root:

```powershell
docker compose up -d --build document-processor-api
```

## Health Check

```powershell
Invoke-RestMethod -Uri http://localhost:8000/health
```

## Extract Example

`path` is resolved inside the container. The compose file mounts the whole project root at `/app/extracted/workspace`, so a path relative to `DOCUMENT_OUTPUT_ROOT` needs a `workspace/` prefix, e.g. `workspace/diavgeia_sample/pdf/6Α8546ΜΤΛ6-Ρ70.pdf` resolves to that mounted file. To read from anywhere else on disk, add another volume mount and reference its container-side path.

```powershell
$body = @{
  path = "workspace/diavgeia_sample/pdf/6Α8546ΜΤΛ6-Ρ70.pdf"
  post_processing = "markdown"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://localhost:8000/extract `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

Response fields:

- `filename` — basename of `path`
- `source_path`, `artifact_id`
- `text_path` — where the (raw/clean/markdown) output was written, e.g. `{DOCUMENT_OUTPUT_ROOT}/{artifact_id}.md`
- `note` — present only when something noteworthy happened, e.g. correction was skipped for being too large, or the correction call failed and the raw text was written instead

## Environment Variables

Optional:

- `DOCUMENT_OUTPUT_ROOT=./extracted`
- `DOCUMENT_EXTRACTION_WORKERS=2`
- `DOCUMENT_OCR_DPI=200`
- `DOCUMENT_OCR_MAX_PAGES=50`
- `DOCUMENT_OCR_TIMEOUT_SECONDS=120`
- `DOCUMENT_CORRECTION_CHUNK_PARALLELISM=10`
- `DOCUMENT_CHUNK_TOKENS=8000` — per-chunk input budget in tokens (see "Large Documents And Chunking")

Required only for `post_processing=clean` or `post_processing=markdown`:

- `OPENAI_API_KEY`
- `OPENAI_MODEL` (default `gpt-4.1`)
- `MAX_TOKENS` (default `4096`) — caps each correction request's output, and also sets the chunking thresholds (see "Large Documents And Chunking")

## OCR Support In Docker Image

The image includes:

- `libreoffice-writer`
- `poppler-utils`
- `tesseract-ocr`
- `tesseract-ocr-eng`
- `tesseract-ocr-ell`
