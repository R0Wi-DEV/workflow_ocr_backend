---
name: add-endpoint
description: Add or change a REST endpoint in workflow_ocr_backend - FastAPI handler, pydantic response model with camelCase aliases, error handling, tests, and the matching client change in the workflow_ocr PHP app. Use when asked to extend the backend API.
---

Every endpoint here exists to serve the `workflow_ocr` PHP app. An endpoint no client calls
is dead code; plan the PHP side in the same change.

## 1. Response model

Add it to `workflow_ocr_backend/model/ocrresult.py` (or a sibling module) as a pydantic
`BaseModel`. Snake_case attributes, explicit `serialization_alias` for the camelCase wire
name, `description` on every field — the descriptions become the OpenAPI docs the PHP
client is generated from by hand.

```python
class MyResult(BaseModel):
    some_value: str = Field(serialization_alias='someValue', description='...')
```

## 2. Handler

In `workflow_ocr_backend/app.py`. Keep it thin — parse and validate here, do the work in
`OcrService` (or a new service class in the package). Declare the response model and the
error response so the OpenAPI schema stays truthful:

```python
@APP.post("/my_endpoint", response_model=MyResult, responses={500: {"model": ErrorResult}})
async def my_endpoint(file: UploadFile = File(..., description="...")):
    service = OcrService(logger)
    return service.do_something(file.file, file.filename)
```

Notes:
- Parameter names are the multipart/form field names on the wire — the PHP client sends
  them literally. Choose them deliberately.
- `AppAPIAuthMiddleware` already covers the new route; do not add auth of your own.
- Do not add per-endpoint `try/except` that swallows errors. The two global handlers in
  `app.py` already turn exceptions into a 500 with `ErrorResult`'s shape, and
  `ocrmypdf.ExitCodeException` into one carrying `ocrMyPdfExitCode`.
- Log via `logging.getLogger('uvicorn.error')`. Never log file contents or base64 payloads.

## 3. Status codes

The PHP client handles exactly 200 and 500 and raises on anything else. If the endpoint
needs another status code, the PHP `ApiClient` needs a matching branch in the same change —
otherwise the failure is opaque on the Nextcloud side.

## 4. Tests

Add to `test/test_app.py`, following the existing shape: `TestClient(APP, headers=headers)`
inside a `with` block so lifespan handlers run, headers built from `.env`. Cover the success
path, a malformed input, and the error path's JSON shape. Fixtures go in `test/testdata/`.

## 5. Client side

In `workflow_ocr`: add the method to `IApiClient`/`ApiClient`, add the PHP model under
`lib/OcrProcessors/Remote/Client/Model/` with matching `$openAPITypes`, `$attributeMap`
and getter/setter maps, and add a unit test. Then run `/sync-backend-contract` to verify
both halves line up.

## 6. Finish

`/preflight`, and document the endpoint in `README.md` (plus an `examples/*.sh` script if
the existing ones would help someone call it by hand).
