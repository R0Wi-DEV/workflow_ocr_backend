---
name: run-local
description: Run workflow_ocr_backend locally or in Docker and register it as an ExApp against a Nextcloud instance so the workflow_ocr app uses the remote backend. Use when asked to start, debug, or manually exercise the backend.
---

## Run it

**Devcontainer** (`.devcontainer/devcontainer.json`, Dockerfile `devcontainer` stage) is
the quickest path: Python 3.12 with `ocrmypdf`, tesseract data, dev deps, and the Docker
socket mounted so the HaRP test can run.

**Directly:**

```bash
make deps
python main.py        # nc_py_api's run_app handles host/port binding from the env
```

Configuration comes from `.env` (dummy values, safe to use locally): `APP_PORT=5000`,
`APP_ID=workflow_ocr_backend`, `APP_SECRET`, `NEXTCLOUD_URL`, `AA_VERSION`.

**In Docker:**

```bash
make build
docker run --rm -p 5000:5000 --env-file .env workflow-ocr-backend
```

Without `HP_SHARED_KEY`, `start.sh` skips the FRP/HaRP setup and just execs the app.

## Call it by hand

Every route sits behind `AppAPIAuthMiddleware`, so requests need the ExApp headers
(`AA-VERSION`, `EX-APP-ID`, `EX-APP-VERSION`, `AUTHORIZATION-APP-API` — base64 of
`<user>:<APP_SECRET>`). `examples/` holds ready-made `curl` snippets to copy and adapt:

- `examples/process-request.sh` — `POST /process_ocr` with a sample PDF and parameters
- `examples/get-langs-request.sh` — `GET /installed_languages`
- `examples/openapi-request.sh` — the OpenAPI schema

`docs` and `openapi.json` are the only unauthenticated paths — `http://localhost:5000/docs`
gives you Swagger UI against the live app.

## Register as an ExApp

`workflow_ocr` only uses the remote backend when `app_api` is enabled **and** this ExApp is
registered and enabled. `examples/register-ex-app.sh` holds the command, which runs
**on the Nextcloud host**:

```bash
php occ app_api:app:register workflow_ocr_backend --force-scopes \
    --info-xml <url-or-path-to-this-repo's-appinfo/info.xml>
php occ app_api:app:list               # confirm it shows as enabled
```

Point `--info-xml` at your local `appinfo/info.xml` when testing an unreleased change; the
committed script points at `master` on GitHub.

Then in `workflow_ocr`, `IOcrBackendInfoService::isRemoteBackend()` flips to true and
`OcrProcessorFactory` starts resolving `WorkflowOcrRemoteProcessor`. Until then every OCR
run uses the local CLI, whatever this container is doing.

## Debugging

- Logs go to the uvicorn logger; `main.py` runs with `log_level="trace"`.
- OCR failures surface as `ocrmypdf.ExitCodeException` → HTTP 500 with `ocrMyPdfExitCode`.
  The exit code is `ocrmypdf`'s own — look it up in the OCRmyPDF docs before assuming the
  bug is here.
- `tesseract --list-langs` inside the container tells you which languages are actually
  installed; the image installs every `tesseract-ocr-data-*` package.
- HaRP/FRP problems: read `start.sh` and `test/test_harp_integration.py`, which spins up the
  whole deployment path and is the most precise description of it.
