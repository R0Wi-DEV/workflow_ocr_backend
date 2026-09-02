# CLAUDE.md — workflow_ocr_backend

Python **ExApp** (Nextcloud External App) that wraps OCRmyPDF behind a small REST API. It
is the "remote backend" for the
[`workflow_ocr`](https://github.com/R0Wi/workflow_ocr) Nextcloud app — that PHP app is the
only client, and its hand-written HTTP client must stay in sync with this API. See
`.claude/skills/sync-backend-contract/`.

## Critical constraints

- **This service is not a general web app.** It runs as an ExApp: Nextcloud's AppAPI
  registers the container, and every request arrives through AppAPI with ExApp auth headers
  (`AppAPIAuthMiddleware`). It is not meant to be exposed directly.
- **The API surface is a contract with the PHP app**, enforced by nothing at build time.
  Endpoint names, multipart field names, and the camelCase JSON aliases are load-bearing —
  changing one silently breaks the remote backend path in `workflow_ocr`.
- **Versions move together.** `appinfo/info.xml` here carries the same version as
  `workflow_ocr`'s `appinfo/info.xml` (with a `-dev` suffix between releases). Currently
  `1.36.0-dev`, NC 36.
- The published image is `ghcr.io/r0wi-dev/workflow_ocr_backend`, pinned in the
  `<external-app><docker-install>` block of `appinfo/info.xml`.

## Stack

Python 3.12 (Alpine) · FastAPI · `nc_py_api[app]` 0.30.1 · `ocrmypdf` 17.4.2 · pydantic ·
`pytest` + `httpx` + `python-dotenv`.

## Layout

```
main.py                          nc_py_api run_app(APP) — binding handled by the library
workflow_ocr_backend/app.py      FastAPI app, middleware, exception handlers, endpoints
workflow_ocr_backend/ocrservice.py   OCRmyPDF wrapper + parameter parsing
workflow_ocr_backend/model/ocrresult.py  pydantic response models
test/test_app.py                 endpoint tests via fastapi TestClient
test/test_harp_integration.py    full HaRP/FRP deployment test (Docker, marked)
examples/                        curl/shell scripts for manual API and ExApp registration
Dockerfile                       app → devcontainer → test build stages
start.sh                         writes /frpc.toml and starts frpc when HP_SHARED_KEY is set
```

## API

| Method | Path | Handler | Notes |
| --- | --- | --- | --- |
| POST | `/process_ocr` | `app.py::process_ocr` | multipart: `file`, `ocrmypdf_parameters` |
| GET | `/installed_languages` | `app.py::installed_languages` | `tesseract --list-langs`, minus header and `osd` |
| GET | `/heartbeat` | provided by `nc_py_api`'s `set_handlers` | used by the PHP app's `ApiClient::heartbeat` |

Responses (`model/ocrresult.py`) serialize with camelCase aliases the PHP client expects:

- `OcrResult` → `filename`, `contentType`, `recognizedText`, `fileContent` (base64 PDF).
- `ErrorResult` → `message`, `ocrMyPdfExitCode`.

Two exception handlers in `app.py` map failures to HTTP 500 with `ErrorResult`'s shape;
`ocrmypdf.ExitCodeException` additionally carries `ocrMyPdfExitCode`. The PHP client
handles **only** 200 and 500 — any other status code becomes an opaque `RuntimeException`
there.

`AppAPIAuthMiddleware` is applied to everything except `docs` and `openapi.json`.

## The `ocrmypdf_parameters` string

`workflow_ocr`'s `CommandLineUtils` sends a CLI-style string
(`--skip-text --language eng+deu --jobs 4`). `OcrService._split_parameters` parses it into
`ocrmypdf.ocr()` **keyword arguments**: split on `--`, `-` → `_` in keys, `+` in a value →
list, numeric → `int`/`float`, `true`/`false` → bool, a bare flag → `True`.

Consequences worth knowing before touching either side:

- Values containing a space or `--` do not survive the parser.
- A flag must exist as an `ocrmypdf.ocr()` keyword, not only as a CLI option.
- Local-only flags (`-q`, `--sidecar`) are filtered out on the PHP side and must never
  arrive here.

`OcrService.ocr` always requests a sidecar into an in-memory buffer, so the recognized text
is returned alongside the PDF.

## Commands

```bash
make deps                 # pip install -r requirements-dev.txt
make test                 # pytest with coverage, excludes the harp_integration marker
make harp-integrationtest # pytest -m harp_integration  (needs a working Docker CLI)
make build                # docker build -t workflow-ocr-backend .
```

Tests read configuration from `.env` (`APP_ID`, `APP_SECRET`, `APP_VERSION`, `AA_VERSION`,
…) via `python-dotenv`; the ExApp auth headers in `test/test_app.py` are built from those
values. `.env` is committed and contains dummy values only — keep it that way.

CI (`.github/workflows/test.yml`) runs the unit suite inside the Dockerfile's `test` stage
and enforces a coverage threshold, then runs the HaRP integration test on a Docker-capable
runner.

## Docker and deployment

- `Dockerfile` has three stages: `app` (runtime: `ocrmypdf`, every
  `tesseract-ocr-data-*` package, `frp`, `gosu`, non-root `serviceuser`), `devcontainer`
  (adds dev deps, `sudo`, docker CLI — used by `.devcontainer/`), and `test` (entrypoint
  `make test`).
- `start.sh` is the entrypoint wrapper: when `HP_SHARED_KEY` is set it generates
  `/frpc.toml` (with TLS when `/certs/frp` exists) and starts `frpc` in the background for
  **HaRP** (Nextcloud 32+ ExApp deployment via FRP over a unix socket at
  `/tmp/exapp.sock`), then execs the app. Without `HP_SHARED_KEY` it just execs the app.
- The app must keep running as `serviceuser`; do not move work back to root.

## Conventions

- Endpoints stay thin: validate/parse in `app.py`, do the work in `OcrService`. Instantiate
  the service per request as the existing handlers do.
- Log through `logging.getLogger('uvicorn.error')` so output lands in the container log
  Nextcloud admins read.
- Never log file contents or the raw base64 payload.
- Response models are pydantic with explicit `serialization_alias` — snake_case in Python,
  camelCase on the wire, always.
- Every new endpoint needs a test in `test/test_app.py` using `TestClient` inside a `with`
  block (so lifespan handlers run) and the ExApp auth headers.

## Tooling in this repo

- `.claude/skills/` — `/add-endpoint`, `/preflight`, `/sync-backend-contract`, `/run-local`.
- `.claude/agents/` — ExApp/FastAPI reviewer and a pytest author.
