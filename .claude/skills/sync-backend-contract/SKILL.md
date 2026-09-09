---
name: sync-backend-contract
description: Verify or update the REST contract between this ExApp and the workflow_ocr PHP app - endpoints, multipart fields, camelCase JSON aliases, status codes, the ocrmypdf parameter string and versions. Use whenever an endpoint, model, error path or deployment detail changes.
---

The PHP app is a hand-written client of this API. Nothing validates the contract at build
time — `workflow_ocr`'s integration suite running with `backend: remote` is the only
end-to-end guard, and it lives in the other repo. Walk all six points whenever either side
changes.

Client repo: `R0Wi/workflow_ocr`. If it is not checked out next to this one, read it via
the GitHub MCP tools rather than assuming.

## 1. Endpoints

| Endpoint | Here | PHP caller |
| --- | --- | --- |
| `POST /process_ocr` | `app.py::process_ocr` | `ApiClient::processOcr` |
| `GET /installed_languages` | `app.py::installed_languages` | `ApiClient::getLanguages` |
| `GET /heartbeat` | `nc_py_api`'s `set_handlers` | `ApiClient::heartbeat` |

The PHP side reaches all of them through `IAppApiWrapper::exAppRequest`, so AppAPI must
know the route — a path change is a deployment change, not just a code change.

## 2. Multipart field names for `/process_ocr`

FastAPI parameter names are the wire field names, and the PHP client hardcodes them:

| Part | Here | PHP |
| --- | --- | --- |
| file | `file: UploadFile = File(...)` | `'name' => 'file'`, `'filename' => $fileName` |
| params | `ocrmypdf_parameters: str = Form(None)` | `'name' => 'ocrmypdf_parameters'` |

Renaming a parameter renames the field. There is no alias.

## 3. JSON field names

`serialization_alias` here, `$openAPITypes` / `$attributeMap` there — two hand-written
halves of one schema.

| Here (`model/ocrresult.py`) | Wire | PHP (`lib/OcrProcessors/Remote/Client/Model/`) |
| --- | --- | --- |
| `OcrResult.filename` | `filename` | `OcrResult` |
| `OcrResult.content_type` | `contentType` | `OcrResult` |
| `OcrResult.recognized_text` | `recognizedText` | `OcrResult` |
| `OcrResult.file_content` (base64) | `fileContent` | `OcrResult` |
| `ErrorResult.message` | `message` | `ErrorResult` |
| `ErrorResult.ocr_my_pdf_exit_code` | `ocrMyPdfExitCode` | `ErrorResult` |

Adding a field means four edits: the pydantic model here, and the PHP model's
`$openAPITypes`, `$openAPIFormats`/`$attributeMap`, and getter/setter maps.

`fileContent` is base64; `WorkflowOcrRemoteProcessor` calls `base64_decode` on it.

## 4. Status codes

`ApiClient::processOcr` branches on exactly `200` → `OcrResult` and `500` → `ErrorResult`,
and throws `RuntimeException` on anything else — which surfaces to the Nextcloud user as a
generic failure with no message. `getLanguages` additionally requires a literal 200.

So: a new status code here needs a matching branch in `ApiClient` in the same change. Both
global exception handlers in `app.py` deliberately return 500 for this reason.

## 5. The `ocrmypdf_parameters` string

PHP's `CommandLineUtils::getCommandlineArgs` emits a CLI-style string;
`OcrService._split_parameters` parses it back into `ocrmypdf.ocr()` kwargs. When changing
the parser or adding a flag, check:

- The flag exists as an `ocrmypdf.ocr()` **keyword**, not only as a CLI option.
- Its value contains no space and no `--` — the parser splits on both.
- `-` in the flag name maps to `_`; `+` in a value produces a list (that is how
  `--language eng+deu` works); numeric strings become `int`/`float` and `true`/`false`
  become bools, which silently changes the type a flag receives.
- Local-only flags (`-q`, `--sidecar`) are gated out on the PHP side by `$isLocalExecution`
  and must never arrive here.

## 6. Versions and deployment

`appinfo/info.xml` here and in `workflow_ocr` carry the same version (`-dev` suffix here
between releases), and the same `<nextcloud min/max-version>`. The
`<external-app><docker-install>` block pins `ghcr.io/r0wi-dev/workflow_ocr_backend` — a
tag change affects what admins actually deploy.

## Verifying

```bash
make test                   # this side
make harp-integrationtest   # if deployment/registration changed
```

and, in `workflow_ocr`, `make php-integrationtest` (the `remote` backend path). If you
cannot run the PHP side, say so explicitly instead of reporting the contract as verified.
