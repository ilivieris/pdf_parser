# Document Processor Service

Minimal document-processing API for the chatbot template.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /extract` | Upload a document, extract its text, get a download link |
| `GET /health` | Readiness: probes MinIO, returns `503` when it is unreachable |
| `GET /health/live` | Liveness: is the process up. Probes nothing, always `200` |

## Contract

`POST /extract` takes a **file upload**, not a path. One request does the whole round trip:

1. The uploaded file is stored in the MinIO bucket under `{MINIO_SOURCE_PREFIX}/{artifact_id}/{filename}`.
2. It is read back **out of the bucket** and parsed from those bytes — the bucket, not the request body,
   is the source of truth for what was processed.
3. The extracted text is written back to the bucket under `{MINIO_OUTPUT_PREFIX}/{artifact_id}{suffix}`,
   and the response carries a presigned download URL for it.

`artifact_id` is a content hash of the upload, so re-uploading the same file overwrites the same
two objects instead of accumulating copies.

Request (`multipart/form-data`):

- `file` — the document. Its extension picks the parser. Supported formats are `PDF`, `DOCX`, `DOC`,
  and the text-like extensions listed in `app/core/config.py`.
- `post_processing` — `none` (default), `clean`, or `markdown`:
  - `none`: the raw extracted text, written as `{artifact_id}.txt`.
  - `clean`: the text is sent to the configured OpenAI model to fix spelling/grammar and improve
    layout, written as `{artifact_id}_clean.txt`.
  - `markdown`: the text is sent to the configured OpenAI model with its own prompt that fixes
    spelling/grammar **and** formats the result as markdown in a single pass (an independent
    correction call, not `clean` followed by a markdown conversion step), written as `{artifact_id}.md`.

Response:

| Field | Meaning |
| --- | --- |
| `filename` | The uploaded file's original name |
| `download_url` | Presigned GET URL for the extracted text, valid for `MINIO_URL_EXPIRY_SECONDS` |
| `note` | Present only when something noteworthy happened — correction skipped for size, or the correction call failed and the raw text was stored instead |

The object keys stay out of the response on purpose: the download link is the only handle a
caller needs, and the bucket layout is an internal detail free to change. Both keys are in the
`document_extract_finished` log line if you need to trace a request to its objects.

Error cases:

- Uploads over `MAX_UPLOAD_SIZE_BYTES` are rejected with `413` **before** anything is stored.
- An unsupported extension returns `400`; a parse failure returns `422`; a MinIO failure returns `502`.
- Extraction failures return an error instead of silently writing empty output.

### The `MINIO_PUBLIC_ENDPOINT` Setting

Presigned URLs are SigV4-signed, and the signature covers the `Host` header
(`X-Amz-SignedHeaders=host`) — a link's host cannot be swapped after signing without producing
`SignatureDoesNotMatch`. So when the caller reaches MinIO under a different name than the service
does (the service dials `minio:9000` inside Compose, the browser needs `localhost:9000`), set
`MINIO_PUBLIC_ENDPOINT` to the caller's name. The service then signs download links with a second
client bound to that host. Leave it empty when both sides use the same address.

`MINIO_REGION` is pinned for the same reason: resolving a bucket's region is a network call to the
endpoint, which the presigning client cannot necessarily reach.

### Cleaning Up Stored Objects

Nothing is deleted by default — both the source and the extracted text stay in the bucket.
Two independent switches change that:

- **`MINIO_DELETE_SOURCE_AFTER_EXTRACT=true`** deletes the uploaded source as soon as its text
  has been stored, within the same request. The output and its download link are unaffected.
  Keeping the source is the default because it is what lets you re-run an extraction (say, with
  a different `post_processing`) without the caller uploading the file again.
- **`MINIO_SOURCE_RETENTION_DAYS` / `MINIO_OUTPUT_RETENTION_DAYS`** expire everything under
  each prefix after N days (`0` = keep forever). These are written to the bucket as S3 lifecycle
  rules on startup and enforced by **MinIO's own scanner**, not by this service — so expiry
  keeps working while the service is down, and it cannot delete an object out from under a
  download link that is still inside its window. Set the output window to at least
  `MINIO_URL_EXPIRY_SECONDS` if you want every issued link to stay valid for its full life.

The service only touches lifecycle rules whose id starts with `document-processor-`; any other
rule already on the bucket is preserved, so the bucket can be shared with other workloads. If the
credentials cannot write lifecycle config, startup logs the failure and continues — retention is
housekeeping, not a reason to refuse traffic.

Note that setting a retention back to `0` does **not** retract a rule already written to the
bucket: with no retention configured the service does not touch lifecycle config at all, so it
never needs write permission you did not ask it to use. Remove a rule that is no longer wanted
with `mc ilm rule rm --id document-processor-sources <alias>/<bucket>`.

There is deliberately no "delete the output immediately" switch: the response *is* a link to that
object, so deleting it would hand back a dead URL. If you want the text to exist only for the
duration of one request, the contract has to change — return the text inline instead of a link.

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
- Markdown export: `extracted/790ff7612eeff714.md` (written to local disk; that run predates the MinIO flow)

Docling status in this runtime:

- `docling` is not installed in the current container, so a fresh docling comparison could not be generated here
- `importlib.util.find_spec("docling") -> None`

If docling is reintroduced later, this section is the place to add a direct side-by-side comparison for the same test file.

## Run With Docker Compose

Fill in the MinIO settings in `.env` first (see `.env_template`), then:

```powershell
docker compose up -d --build document-processor-api
```

The service does not bundle a MinIO — point `MINIO_ENDPOINT` at your own. The bucket named by
`MINIO_BUCKET` is created on startup if it does not already exist.

## Health Checks

`GET /health` is the **readiness** probe. It round-trips to MinIO, times the call, and returns
`503` with the same body shape when the probe fails, so a load balancer can drain the instance:

```powershell
Invoke-RestMethod -Uri http://localhost:8000/health
```

```json
{
  "status": "healthy",
  "service": "document_processor_service",
  "version": "0.3.0",
  "uptime_seconds": 42.317,
  "checks": [
    { "name": "minio:localhost:9000/documents", "status": "pass", "latency_ms": 3.41, "detail": null }
  ]
}
```

When MinIO is unreachable the response is `503`, `status` is `degraded`, and the failing check
carries the reason in `detail`.

`GET /health/live` is the **liveness** probe. It probes nothing and always returns `200` while
the process is running. Keep the two separate: pointing a liveness probe at MinIO would restart
healthy containers during a storage outage and turn it into a crash loop.

## Extract Example

```powershell
$result = Invoke-RestMethod `
  -Uri http://localhost:8000/extract `
  -Method Post `
  -Form @{
    file            = Get-Item "diavgeia_sample/test/1.pdf"
    post_processing = "none"
  }

$result | Format-List
Invoke-WebRequest -Uri $result.download_url -OutFile "1.txt"
```

The response is `filename`, `download_url`, and `note` — pass `download_url` straight to whatever
needs the text.

With `curl`:

```bash
curl -X POST http://localhost:8000/extract \
  -F "file=@diavgeia_sample/test/1.pdf" \
  -F "post_processing=none"
```

`diavgeia_parsing.py` does the same for every PDF under `diavgeia_sample/test/`.

## Environment Variables

MinIO (required):

- `MINIO_ENDPOINT` — `host:port`, or a full `http(s)://` URL (the scheme then sets `MINIO_SECURE`)
- `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`
- `MINIO_BUCKET` (default `documents`) — created on startup if missing

MinIO (optional):

- `MINIO_SECURE=false` — `true` when MinIO is served over TLS
- `MINIO_REGION=us-east-1` — pinned rather than looked up (see above)
- `MINIO_PUBLIC_ENDPOINT=` — the host to sign download links for, when it differs from `MINIO_ENDPOINT`
- `MINIO_URL_EXPIRY_SECONDS=604800` — download link lifetime; 7 days is the presign maximum
- `MINIO_SOURCE_PREFIX=sources`, `MINIO_OUTPUT_PREFIX=extracted`
- `MAX_UPLOAD_SIZE_BYTES=26214400` — larger uploads are rejected with `413`

Cleanup (optional, see "Cleaning Up Stored Objects"):

- `MINIO_DELETE_SOURCE_AFTER_EXTRACT=false` — drop the upload once its text is stored
- `MINIO_SOURCE_RETENTION_DAYS=0`, `MINIO_OUTPUT_RETENTION_DAYS=0` — expire after N days; `0` keeps forever

Extraction (optional):

- `DOCUMENT_EXTRACTION_WORKERS=2`
- `DOCUMENT_OCR_DPI=200`
- `DOCUMENT_OCR_MAX_PAGES=50`
- `DOCUMENT_OCR_TIMEOUT_SECONDS=120`
- `DOCUMENT_CORRECTION_CHUNK_PARALLELISM=10`
- `DOCUMENT_CHUNK_TOKENS=8000` — per-chunk input budget in tokens (see "Large Documents And Chunking")

Required only for `post_processing=clean` or `post_processing=markdown`:

- `OPENAI_API_KEY`
- `OPENAI_MODEL` (default `gpt-4.1`)
- `MAX_TOKENS` (default `32768`) — caps each correction request's output, and also sets the chunking thresholds (see "Large Documents And Chunking")

Values in `.env` must be **unquoted** — pydantic strips surrounding quotes, but Docker's `env_file`
passes them through literally and the container then fails to parse numeric settings.

## OCR Support In Docker Image

The image includes:

- `libreoffice-writer`
- `poppler-utils`
- `tesseract-ocr`
- `tesseract-ocr-eng`
- `tesseract-ocr-ell`
