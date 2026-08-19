# Code Review — Workflow OCR Backend

**Scope:** the whole application at commit `7579129` — `main.py`, `workflow_ocr_backend/`, `test/`, `Dockerfile`, `start.sh`, `.github/`, packaging and configuration.
**Focus:** security and coding best practices.
**Method:** source reading, plus behavioural verification against the pinned dependency versions (`ocrmypdf==17.4.2`, `nc-py-api==0.30.1`, uvicorn). Every claim marked *verified* below was reproduced, not inferred.

---

## Summary

The app is small, readable and does one thing. The structure (thin FastAPI layer → `OcrService` → `ocrmypdf`) is the right shape, the HaRP/FRP integration is carefully done, and the Docker build gets some things right that most projects get wrong (gosu pinned *and* GPG-verified, the sudo-enabled `devcontainer`/`test` stages deliberately excluded from the published `app` target).

The dominant problem is a single design decision: **the `ocrmypdf_parameters` form field is parsed into a `dict` and splatted into `ocrmypdf.ocr(**kwargs)` with no allowlist.** That one line is the root of the critical finding and of eight of the twelve correctness bugs. Fixing it properly fixes most of this report.

The second theme is that the service has **no resource ceiling of any kind** — no upload size limit, no OCR timeout, no concurrency bound — and it does its CPU-bound work on the asyncio event loop, so a single large document makes the whole process, including `/heartbeat`, unresponsive.

| Severity | Count |
|---|---|
| Critical | 1 |
| High | 3 |
| Medium | 5 |
| Low / correctness | 12 |
| Best practice | 12 |

---

## Critical

### SEC-1 — Caller-controlled `ocrmypdf` kwargs allow arbitrary Python import and code execution

`workflow_ocr_backend/ocrservice.py:24-25`

```python
kwargs = self._split_parameters(ocrmypdf_parameters)
exit_code = ocrmypdf.ocr(file, output_buffer, sidecar=sidecar_buffer, progress_bar=False, **kwargs)
```

`_split_parameters` accepts *any* key. `ocrmypdf.ocr()` accepts a `plugins` parameter, and `OcrmypdfPluginManager._setup_plugins` resolves it like this (`ocrmypdf/_plugin_manager.py:96-106`):

```python
for name in self._plugins:
    if isinstance(name, Path) or name.endswith('.py'):
        spec = importlib.util.spec_from_file_location(module_name, name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)          # <- executes the file
    else:
        module = importlib.import_module(name)   # <- imports any installed module
```

`ocrmypdf.api.ocr` normalises a bare string to a one-element list (`if isinstance(plugins, str | Path): plugins = [plugins]`), so a scalar works.

**Verified:**

```
_split_parameters("--plugins /tmp/evil.py")  ->  {'plugins': '/tmp/evil.py'}
```

Which reaches `exec_module()` on that path.

**Impact.** Any caller who can reach `/process_ocr` gets:

1. **Arbitrary Python module import** by dotted name — unconditional, requiring nothing but the request. Import side effects run in the ExApp process.
2. **Arbitrary code execution** as `serviceuser` in the container, as soon as any `.py` file exists at a path the attacker can name — a mounted volume, a shared data directory, a file planted through any other route.

**Caveat, stated honestly:** the endpoint sits behind `AppAPIAuthMiddleware`, so the caller must already be authenticated as Nextcloud. This is not a pre-auth internet-facing RCE. It is a privilege-boundary failure: the OCR backend is supposed to be a sandboxed document processor, and instead any component that can submit a document can execute code inside it. In the intended `workflow_ocr` deployment, the parameter string originates from a *per-workflow admin setting*, which makes this at minimum an admin → container-RCE escalation, and a full RCE for any path where those parameters become user-influenced.

Related dangerous keys reachable the same way: `user_words` / `user_patterns` (arbitrary local file paths handed to tesseract), `plugin_manager`, `keep_temporary_files`, `output_file`.

**Fix:** a strict allowlist — see the plan, item P0.

---

## High

### SEC-2 — The same pass-through disables ocrmypdf's own DoS guards

`ocrmypdf` ships defensive defaults. All of them are caller-overridable here. **Verified:**

```
_split_parameters("--max-image-mpixels 0")  ->  {'max_image_mpixels': 0}   # decompression-bomb guard OFF
_split_parameters("--jobs 9999")            ->  {'jobs': 9999}             # unbounded worker fan-out
_split_parameters("--keep-temporary-files") ->  {'keep_temporary_files': True}  # fills the container disk
```

A small crafted PDF plus `--max-image-mpixels 0` is enough to exhaust container memory. `--jobs` at a large value fans out subprocesses against a container that has no cgroup limits declared. `--keep-temporary-files` leaves every intermediate raster on disk, permanently, across requests.

Even absent SEC-1, the parameter surface must be an allowlist with *bounds*, not just names.

### SEC-3 — No size limits anywhere; peak memory is a multiple of the document

`workflow_ocr_backend/ocrservice.py:17-39`, `workflow_ocr_backend/app.py:43-53`

The whole pipeline is in-memory and copies repeatedly:

1. Starlette spools the upload (memory, then a temp file past its threshold).
2. `ocrmypdf` writes the output PDF into an in-memory `BytesIO`.
3. `base64.b64encode(output_buffer.getvalue())` — a full copy, +33%.
4. `.decode("utf-8")` — another full copy.
5. FastAPI/pydantic serialises it into a JSON response — another copy.

Peak resident memory is roughly 4–5× the output document, and there is **no maximum upload size** at the app, at uvicorn, or in the ExApp deployment. Nothing rejects a 500 MB PDF.

Compounding it: `ocrmypdf.api` holds a process-global `threading.Lock` (`_api_lock`, `api.py:69`) around the entire pipeline run, so requests are already serialised to one at a time — but nothing *rejects* the queued ones, they simply accumulate, each holding its uploaded bytes.

### SEC-4 — Blocking CPU work on the asyncio event loop stalls the whole process, including `/heartbeat`

`workflow_ocr_backend/app.py:44-53`

```python
async def process_ocr(...):
    service = OcrService(logger)
    return service.ocr(file.file, file.filename, ocrmypdf_parameters)   # fully synchronous
```

The handler is `async def` but its body is entirely blocking — `ocrmypdf.ocr()` is synchronous, CPU-bound, and can run for minutes. Declaring it `async` means it runs *on the event loop* rather than in the threadpool, so for the duration of an OCR run the process serves nothing else.

`nc_py_api`'s `set_handlers` registers `/heartbeat` (`integration_fastapi.py:144-147`). AppAPI polls it. While a large document is processing, that poll gets no response, and AppAPI concludes the ExApp is dead.

Note the inversion: `installed_languages` is declared `def` (sync), so FastAPI *does* run it in the threadpool. The cheap endpoint is offloaded and the expensive one is not — this looks like an oversight rather than a decision.

**Fix:** `def process_ocr(...)` (FastAPI offloads it automatically), or `await run_in_threadpool(...)`, combined with an explicit `asyncio.Semaphore`, a request timeout, and a `tesseract_timeout` floor.

---

## Medium

### SEC-5 — Arbitrary local file paths via `user_words` / `user_patterns`

`ocrmypdf.ocr()` takes `user_words: os.PathLike` and `user_patterns: os.PathLike` and hands them to tesseract. **Verified:** `_split_parameters("--user-words /etc/passwd") -> {'user_words': '/etc/passwd'}`. This yields file-existence probing inside the container and, depending on tesseract's parsing, limited content influence on the returned `recognizedText`. Same root cause as SEC-1; listed separately because it survives any fix that only blocks `plugins`.

### SEC-6 — Internal exception detail returned to the caller

`workflow_ocr_backend/app.py:32-40`

```python
@APP.exception_handler(Exception)
async def exception_handler(_: Request, exc: Exception):
    return JSONResponse({"message": f"{str(exc)} ({exc.__class__.__name__})"}, status_code=500)
```

*Every* unhandled exception — including ones that have nothing to do with OCR — has its message and class name returned over HTTP. Exception strings routinely carry absolute temp paths, library internals, and partial input. The existing tests show it working as designed for `ocrmypdf` errors, but the catch-all `Exception` handler applies the same treatment to `TypeError`, `OSError`, `UnicodeDecodeError` and anything else.

The `ExitCodeException` handler is a different case and should be kept — the PHP `workflow_ocr` client depends on `message` + `ocrMyPdfExitCode`. The fix is to keep that contract and make the generic handler return a fixed string plus a correlation id, with the full detail logged server-side.

### SEC-7 — Unsanitised filename in logs and in the response

`workflow_ocr_backend/ocrservice.py:22,37,39`

`file.filename` is fully attacker-controlled and is (a) interpolated into log lines and (b) echoed back verbatim as `OcrResult.filename`.

- **Log injection:** a filename containing `\r\n` forges log entries. With `log_level="trace"` (see BP-1) these lines are always emitted.
- **Downstream path handling:** the consuming Nextcloud app receives whatever was sent. A filename of `../../foo.pdf` is echoed unchanged; whether that matters depends on the client, which is exactly why the boundary should sanitise rather than assume.

The same applies to `ocrmypdf_parameters`, which is logged raw at line 22.

**Fix:** `os.path.basename()`, strip control characters, cap length, and use structured logging (`logger.debug("Processing %s", name)`) rather than f-strings.

### SEC-8 — `/docs` and `/openapi.json` are unauthenticated

`workflow_ocr_backend/app.py:23`

```python
APP.add_middleware(AppAPIAuthMiddleware, disable_for=["docs", "openapi.json"])
```

`AppAPIAuthMiddleware` matches with `fnmatch` on the stripped path (`integration_fastapi.py:365-366`), so the exemption is exactly those two paths — no wildcard hazard. But both are served without authentication on the ExApp port, exposing the full API schema and an interactive request builder to anyone who can reach it. In HaRP deployments that's whoever reaches HaRP; in docker-socket-proxy deployments it's the Docker network.

The schema is not secret, but it is free reconnaissance for SEC-1. **Fix:** gate `docs_url`/`openapi_url` behind an env flag, default off in production.

### SEC-9 — Supply chain and release integrity

Several independent gaps, grouped because they share a fix strategy:

- **`appinfo/info.xml:34` — `<image-tag>master</image-tag>`.** Every Nextcloud installation pulls a *mutable* tag. There is no way to pin, audit, or roll back a deployed version, and a compromised or simply broken `master` build propagates to all installs on next pull. The release workflow even extracts this literal string as its "version" (`appstore-build-publish.yml:47`).
- **No transitive dependency pinning.** `requirements.txt` pins three direct deps exactly; everything underneath floats. Builds are not reproducible and a compromised transitive release lands silently. Use a compiled lock file with `--require-hashes`.
- **Actions pinned by tag, not SHA.** `actions/checkout@v4`, `docker/build-push-action@v6`, `svenstaro/upload-release-action@v2`, `irongut/CodeCoverageSummary@v1.3.0`, `R0Wi/nextcloud-appstore-push-action@v1`. Mutable refs in a workflow that holds `APPSTORE_TOKEN` and `APP_PRIVATE_KEY`.
- **No `permissions:` block** in any workflow — `GITHUB_TOKEN` runs at the repository default rather than least privilege, in jobs that push to GHCR and publish releases.
- **Base image not digest-pinned** (`python:3.12-alpine`).
- **No automated scanning:** no Dependabot config, no CodeQL, no container image scan.

---

## Low / correctness

All of the following were reproduced against the current `_split_parameters`.

| ID | Issue | Evidence |
|---|---|---|
| BUG-1 | Multi-token values are silently truncated to the first token | `--title Hello World` → `{'title': 'Hello'}`; `--tesseract-config a b c` → `{'tesseract_config': 'a'}` |
| BUG-2 | `str.isnumeric()` is true for Unicode numerics, then `int()` raises → unhandled 500 | `--oversample ²` → `ValueError: invalid literal for int()` |
| BUG-3 | Negative numbers are never coerced; `--` inside a value corrupts the parse | `--skip-big -1` → `{'skip_big': '-1'}` (string); `--pages 1--2` → `{'pages': 1, '2': True}` |
| BUG-4 | Any value containing `+` becomes a list, even where a scalar is expected | `--title a+b` → `{'title': ['a', 'b']}` |
| BUG-5 | Duplicate keys silently overwrite instead of erroring | `--language eng --language deu` → `{'language': 'deu'}` |
| BUG-6 | Misspelled parameters are silently discarded by `ocrmypdf` into `extra_attrs` — no error, no effect | `--languge eng` is a no-op with zero feedback |
| BUG-7 | `--sidecar …` collides with the hardcoded `sidecar=` kwarg → `TypeError: got multiple values for keyword argument` → 500 | `_split_parameters("--sidecar /tmp/x.txt")` → `{'sidecar': ...}` |
| BUG-8 | `installed_languages` runs `subprocess.run` with no `check=` and no `timeout=`; a tesseract failure returns `[]`, indistinguishable from "no languages installed"; the unconditional `[1:]` header-skip is brittle | `ocrservice.py:46-48` |
| BUG-9 | `UploadFile.filename` is `str \| None`; a multipart part without a filename → pydantic `ValidationError` → 500 | `OcrResult.filename: str` |
| BUG-10 | `sidecar_buffer.getvalue().decode("utf-8")` can raise `UnicodeDecodeError` on unusual tesseract output → 500 | `ocrservice.py:33` |
| BUG-11 | Type annotations claim `str` where `None` is the documented default | `ocrmypdf_parameters: str = Form(None)` in `app.py:46`, and `ocrservice.py:16,50` |
| BUG-12 | `output_buffer.close()` is called twice (line 31 and again in `finally`) | harmless for `BytesIO`, but the cleanup path is untidy |

---

## Best practices

| ID | Observation |
|---|---|
| BP-1 | `main.py:6` hardcodes `log_level="trace"`. This activates uvicorn's `MessageLoggerMiddleware`, which logs an entry per ASGI message per request. *Checked:* it replaces headers and bodies with placeholders, so this is **not** a credential leak — it is log volume and disk pressure in production, plus it guarantees the unsanitised `logger.debug` lines from SEC-7 are always emitted. Make it env-driven, default `info`. |
| BP-2 | `app.py:24` — `logging.getLogger('uvicorn.error')` couples application code to the server implementation; logs vanish silently under any other runner. Use `getLogger(__name__)` and configure handlers at the edge. |
| BP-3 | No `__init__.py` in `workflow_ocr_backend/` or `workflow_ocr_backend/model/` — implicit namespace packages. Works at runtime; fragile for coverage attribution and packaging. |
| BP-4 | No linter, formatter or type checker anywhere (`ruff`, `mypy`). Several findings here (BUG-9, BUG-11) are exactly what a type checker reports for free. |
| BP-5 | **`_split_parameters` has no unit tests at all.** The single highest-risk function in the codebase is covered only incidentally through slow end-to-end OCR runs. There is also no test asserting that an unauthenticated request is rejected. |
| BP-6 | `.env` is committed with `APP_SECRET=secret` and `APP_HOST=0.0.0.0`, loaded with `override=True` at test-module import time, and `COPY`'d into the test image. The values are dummies, but the pattern trains everyone to keep real secrets there. Ship `.env.example`, gitignore `.env`. |
| BP-7 | Dockerfile: `apk update` is redundant alongside `--no-cache`; `apk search tesseract-ocr-data-` installs *every* tesseract language pack, making the image very large, build-time network-dependent and non-reproducible; `pip install` has no `--no-cache-dir`; no `HEALTHCHECK`; base image not digest-pinned. **Credit where due:** gosu is version-pinned and GPG-verified, and the published `app` target correctly excludes the passwordless-sudo `devcontainer` and `test` stages. |
| BP-8 | `start.sh`: `set -e` without `-u`/`pipefail`; env vars are interpolated into TOML unquoted and unvalidated (an unset `HP_FRP_PORT` emits `serverPort = `, invalid TOML); `frpc` is backgrounded with no supervision, so if the tunnel dies the app keeps serving into nothing; `echo "Starting application: $@"` should be `$*`. |
| BP-9 | `ErrorResult` is declared and referenced in `responses={500: ...}` but never used to *build* a response — both handlers hand-roll dicts. The model and the wire format can drift apart with nothing to catch it. |
| BP-10 | `test.yml` builds and runs repository code in a job where the HaRP container receives `/var/run/docker.sock`. On ephemeral GitHub-hosted runners with `pull_request` (no secrets, read-only token) this is contained. It becomes a critical runner escape the day this moves to a self-hosted runner — worth documenting as a hard constraint on the workflow. |
| BP-11 | `info.xml` carries `<version>1.35.0-dev</version>` on `master`, and the release workflow publishes straight from it. |
| BP-12 | No `SECURITY.md` / disclosure policy for an app distributed through the Nextcloud appstore. |

---

## Prioritized plan

Ordered by risk reduced per unit of work. P0 is the one that matters most: it is a single self-contained change that closes the critical finding, both resource-guard bypasses, and seven of the twelve correctness bugs.

### P0 — Replace `_split_parameters` with a validating allowlist parser

**Closes:** SEC-1 (critical), SEC-2, SEC-5, BUG-1 … BUG-7.

**Why:** the vulnerability is not "`plugins` is dangerous" — it is that the function is a *denylist of nothing*. Blocking `plugins` by name leaves `user_words`, `plugin_manager`, `keep_temporary_files`, and whatever the next `ocrmypdf` release adds. The only durable fix is to enumerate what is permitted, with types and bounds, and reject everything else.

**Shape of the change**, in `ocrservice.py`:

```python
# Exhaustive allowlist. Anything absent is rejected with 400 — notably
# plugins, plugin_manager, user_words, user_patterns, sidecar, output_file,
# input_file and keep_temporary_files.
_ALLOWED: dict[str, _Spec] = {
    "language":              _Spec(list_of=str, pattern=r"\A[a-z]{3}(_[a-z]+)?\Z", max_items=8),
    "image_dpi":             _Spec(int, lo=50, hi=1200),
    "oversample":            _Spec(int, lo=0, hi=1200),
    "jobs":                  _Spec(int, lo=1, hi=os.cpu_count() or 4),
    "max_image_mpixels":     _Spec(float, lo=1, hi=500),        # lower bound: never 0
    "skip_big":              _Spec(float, lo=0, hi=10_000),
    "optimize":              _Spec(int, lo=0, hi=3),
    "tesseract_pagesegmode": _Spec(int, lo=0, hi=13),
    "tesseract_oem":         _Spec(int, lo=0, hi=3),
    "tesseract_timeout":     _Spec(float, lo=0, hi=MAX_TESSERACT_TIMEOUT),
    "mode":                  _Spec(str, choices={"force", "skip", "redo"}),
    "output_type":           _Spec(str, choices={"pdf", "pdfa", "pdfa-1", "pdfa-2", "pdfa-3"}),
    "rotate_pages":          _Spec(bool),
    "deskew":                _Spec(bool),
    "clean":                 _Spec(bool),
    "remove_background":     _Spec(bool),
    "force_ocr":             _Spec(bool),
    "skip_text":             _Spec(bool),
    "redo_ocr":              _Spec(bool),
    # ... extend deliberately, one reviewed entry at a time
}
```

Three rules alongside it:

1. **Tokenise with `shlex.split()`**, not `split("--")`. That alone fixes BUG-1 (truncation), BUG-3 (`1--2`) and quoting generally.
2. **Reject, don't ignore.** An unknown or out-of-range parameter returns `400` with a message naming the offender. Silent no-ops (BUG-6) are worse than errors: an admin sets `--languge deu`, sees no error, and ships broken OCR.
3. **Reject duplicates** (BUG-5) and any key that collides with a kwarg the service sets itself (BUG-7).

Cover it with a real unit test table — this is where BP-5 gets paid off, and these tests run in milliseconds, unlike the current end-to-end suite.

### P1 — Put a ceiling on every resource

**Closes:** SEC-3, SEC-4, BUG-8.

1. Change `async def process_ocr` to `def process_ocr` so FastAPI runs it in the threadpool. One keyword; it stops OCR from blocking `/heartbeat`, which is the difference between "slow" and "AppAPI restarts the container".
2. Enforce a **maximum upload size** — read `Content-Length`, reject over the limit before touching the body, and make the limit an env var with a sane default. Also stream the upload to a `NamedTemporaryFile` and hand `ocrmypdf` a path rather than holding it in memory.
3. Bound **concurrency** with an `asyncio.Semaphore` sized to the container's CPU budget, returning `503` when saturated rather than queueing unboundedly. `ocrmypdf`'s global `_api_lock` already serialises the work; this makes the backpressure explicit instead of accidental.
4. Apply a **wall-clock timeout** to the OCR run and a default `tesseract_timeout`.
5. Give `installed_languages`' `subprocess.run` a `timeout=` and `check=True`, and surface a failure as an error rather than an empty list. Cache the result — the language set cannot change while the container runs.

### P2 — Tighten the response and logging boundary

**Closes:** SEC-6, SEC-7, BUG-9 … BUG-12, BP-2, BP-9.

- Generic exception handler returns a fixed message plus a correlation id; the detail goes to the log. Keep the `ExitCodeException` handler's `message` + `ocrMyPdfExitCode` contract intact — the PHP client depends on it — and build both responses *through* `ErrorResult` so the model can't drift from the wire format.
- Sanitise `file.filename`: `os.path.basename`, strip control characters, cap the length, fall back to a generated name when it's `None`.
- Switch to structured logging (`logger.debug("...", name)`), which also removes the log-injection vector.
- `getLogger(__name__)`; fix the `str | None` annotations; `decode("utf-8", errors="replace")`; drop the duplicate `close()`.

### P3 — Supply chain and release integrity

**Closes:** SEC-9, BP-7, BP-10.

- **Publish immutable image tags.** Change `<image-tag>` to a real version and cut a new image per release. This is the single highest-value item in this phase: today there is no such thing as "the version I have installed".
- Compile a hash-pinned lock file; install with `--require-hashes`.
- Pin every GitHub Action to a full commit SHA.
- Add an explicit least-privilege `permissions:` block to each workflow.
- Digest-pin the base image; add `--no-cache-dir`, `PIP_NO_CACHE_DIR`, and a `HEALTHCHECK`.
- Narrow the tesseract language-pack install to a declared set, or accept the image size as a documented trade-off — but stop deriving it from a live `apk search` at build time.
- Add Dependabot, CodeQL, and a container scan; document the self-hosted-runner constraint on the HaRP job.

### P4 — Tooling and hygiene

**Closes:** BP-1, BP-3, BP-4, BP-6, BP-8, BP-11, BP-12.

- `log_level` from the environment, default `info`.
- Add `ruff` + `mypy` with a CI gate. Add `__init__.py` files.
- Replace the committed `.env` with `.env.example`; gitignore `.env`.
- `start.sh`: `set -euo pipefail`, validate required env vars before writing TOML, supervise or `exec` the frpc process, `"$*"` in the echo.
- `SECURITY.md` with a disclosure address.

---

## What's already good

Worth stating, because a review that only lists problems misrepresents the codebase:

- The layering is clean — the FastAPI module has no OCR logic and `OcrService` has no HTTP concerns.
- gosu is version-pinned *and* GPG signature-verified, which is rarer than it should be.
- The multi-stage Dockerfile deliberately keeps the passwordless-sudo `devcontainer`/`test` stages out of the published `app` image.
- The HaRP integration test is genuinely thorough: it stands up a real HaRP container, drives the real ExApp lifecycle, asserts on the generated `frpc.toml`, and cleans up in a `finally`.
- Direct dependencies are pinned to exact versions.
- CI runs tests in the same container image that ships, which eliminates a whole class of "works on my machine".
