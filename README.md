# Document Processor Service

Minimal document-processing API for the chatbot template.

## Endpoints

- `GET /health`
- `POST /extract`

## Contract

- `/extract` loads a file from local disk using `path`. `path` may be an absolute filesystem path, or a path relative to `DOCUMENT_OUTPUT_ROOT`.
- When running via Docker Compose, `path` is resolved **inside the container**, so the source file must be reachable through a mounted volume (see `docker-compose.yml`).
- `filename` is optional. It's only needed when `path` itself has no file extension (some Diavgeia-style keys don't); it tells the parser which format to use and names the output file. If `path` already has an extension and `filename` is also given, their extensions must match.
- Supported extraction formats are `PDF`, `DOCX`, `DOC`, and text-like files listed in `core/config.py`.
- `post_processing` controls what gets written back to disk (under `DOCUMENT_OUTPUT_ROOT`):
  - `none` (default): the raw extracted text, as-is.
  - `clean`: the text is sent to the configured OpenAI model to fix spelling/grammar and improve layout, output as plain text.
  - `markdown`: the text is sent to the configured OpenAI model with its own prompt that fixes spelling/grammar **and** formats the result as markdown in a single pass — it's an independent correction call, not `clean` followed by a markdown conversion step.
- If a `clean`/`markdown` request's text is too large for the configured OpenAI model (see "Large documents" below), correction is skipped and the response's `note` field explains why; the raw extracted text is returned instead.
- Extraction failures return an error instead of silently writing empty output.

## Large Documents And Chunking

Text correction chunks by `MAX_TOKENS`:

- If the document is **≤ `MAX_TOKENS`**, it's corrected in a single OpenAI request.
- If it's **between `MAX_TOKENS` and `10 × MAX_TOKENS`**, it's split into paragraph-aligned chunks and corrected in parallel (`DOCUMENT_CORRECTION_CHUNK_PARALLELISM` concurrent requests), then the corrected chunks are joined back together in order.
- If it's **over `10 × MAX_TOKENS`**, correction is skipped entirely and `note` in the response explains that it was too large.

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
- Markdown export: `extracted/markdown/790ff7612eeff714/notes.md`

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

`path` is resolved inside the container. The compose file mounts `./diavgeia_sample` at `/app/extracted/diavgeia_sample`, so a path relative to `DOCUMENT_OUTPUT_ROOT` such as `diavgeia_sample/pdf/6Α8546ΜΤΛ6-Ρ70.pdf` resolves to that mounted file. To read from anywhere else on disk, add another volume mount and reference its container-side path.

```powershell
$body = @{
  path = "diavgeia_sample/pdf/6Α8546ΜΤΛ6-Ρ70.pdf"
  post_processing = "markdown"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri http://localhost:8000/extract `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

Response fields:

- `filename`, `source_path`, `artifact_id`
- `text_path` — where the (raw/clean/markdown) output was written
- `note` — present only when something noteworthy happened, e.g. correction was skipped for being too large

## Environment Variables

Optional:

- `DOCUMENT_OUTPUT_ROOT=./extracted`
- `DOCUMENT_EXTRACTION_WORKERS=2`
- `DOCUMENT_OCR_DPI=200`
- `DOCUMENT_OCR_MAX_PAGES=50`
- `DOCUMENT_OCR_TIMEOUT_SECONDS=120`
- `DOCUMENT_CORRECTION_CHUNK_PARALLELISM=4`

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
