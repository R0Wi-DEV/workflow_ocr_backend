# Code Review — Workflow OCR Backend

**Scope:** the whole application — `main.py`, `workflow_ocr_backend/`, `test/`, `Dockerfile`, `start.sh`, `.github/`, packaging and configuration.
**Baseline:** PR #12 (`bugfix/security-enhancements`), plus the follow-up commit on this branch.
**Focus:** security and coding best practices.
**Method:** source reading, plus behavioural verification against the pinned dependency versions (`ocrmypdf==17.4.2`, `nc-py-api==0.30.1`, uvicorn). Every claim marked *verified* was reproduced by execution, not inferred.

---

## Summary

The first pass of this review found a critical RCE: `ocrmypdf_parameters` was parsed into a dict and splatted into `ocrmypdf.ocr(**kwargs)` with no allow-list, reaching ocrmypdf's `plugins` parameter and from there `spec.loader.exec_module()`.

**PR #12 closes it.** `plugins` and `plugin_manager` are blocked, and the accompanying test asserts the exploit's marker file is never written rather than merely checking for a 400 — the right kind of test for a code-execution fix.

PR #12 also introduced four defects of its own, because its allow-list was derived at import time from `inspect.signature(ocrmypdf.ocr)` — that is, from **Python keyword names** — while callers send **CLI option names**. Those two sets are not the same. The follow-up commit on this branch fixes all four. They are recorded in full below, because the reasoning matters more than the patch.

What remains is what the first pass called P1 onward: the service still has **no resource ceiling of any kind** — no upload size limit, no OCR timeout, no concurrency bound — and it still does CPU-bound work on the asyncio event loop, so a single large document makes the whole process, including `/heartbeat`, unresponsive.

| | Count |
|---|---|
| Closed by PR #12 | 4 (incl. the critical) |
| Introduced by PR #12, fixed on this branch | 5 |
| Security findings still open | 7 |
| Correctness bugs still open | 10 |
| Best-practice items still open | 12 |

---

## Closed by PR #12

| ID | Finding | How |
|---|---|---|
| SEC-1 | **Critical** — arbitrary Python import and code execution via `--plugins` | `plugins` / `plugin_manager` blocked; test asserts the marker file is never created |
| SEC-5 | Arbitrary local file paths via `--user-words` / `--user-patterns` | both blocked |
| BUG-6 | Misspelled parameters silently discarded into `extra_attrs` — no error, no effect | unknown parameters now return HTTP 400 |
| BUG-7 | `--sidecar` collided with the hardcoded `sidecar=` kwarg → `TypeError` → 500 | `sidecar` blocked |

Also verified correct in #12, for the record: `InvalidOcrParameterError` resolves ahead of the generic `Exception` handler via Starlette's MRO lookup, so it genuinely returns 400 rather than 500; and the language regex rejects every injection form in its test matrix.

---

## Introduced by PR #12 — fixed on this branch

### PR-1 — Allow-list keyed on Python names, not CLI names (HIGH, a real regression)

The allow-list was `inspect.signature(ocrmypdf.ocr)` keyword-only parameters. Callers send CLI option names. Verified by execution against the real ocrmypdf 17.4.2:

| Sent by caller | On PR #12 | Before PR #12 |
|---|---|---|
| `--ocr-engine none` | **400 Unknown parameter** | **worked** — `ocr_engine` is a real `OcrOptions` model field (`_options.py:197`), so `**kwargs` → `create_options` set it |
| `--jpeg-quality 80` | **400 Unknown parameter** | silently ignored (routed to `extra_attrs`) |
| `--jpg-quality 80` | passed | passed |

`--jpeg-quality` is the *primary documented* CLI flag; `--jpg-quality` is its `argparse.SUPPRESS`ed alias (`builtin_plugins/optimize.py:74,86`). The signature exposes only `jpg_quality`, so the allow-list accepted the hidden alias and rejected the documented spelling.

`--ocr-engine none` is the sharper case: a documented flag (`cli.py:413`) that **worked before and failed every job after**.

**Fix:** an explicit literal allow-list of 49 CLI option names, plus an alias map (`jpeg_quality → jpg_quality`) applied after validation, plus an `IGNORED_PARAMETERS` set for CLI-only flags (`--quiet`, `--verbose`, `--no-progress-bar`) that are accepted and dropped rather than rejected, so existing configurations carrying them keep working.

### PR-2 — Allow-list auto-widened on every dependency bump (MEDIUM)

The comment claimed future dangerous options could not be smuggled in. The code did the opposite: because the set was introspected from the *installed* ocrmypdf, any keyword-only parameter a future release adds would be accepted automatically, unreviewed.

**Fix:** the explicit literal set above. Introspection is retained as a *test-time drift guard* (`test_allowed_parameters_still_resolve_against_installed_ocrmypdf`) asserting every allow-listed name still resolves against the installed library — so the list stays reviewed, but a rename or removal upstream fails loudly instead of silently 400ing at runtime.

### PR-3 — `tesseract_config` left allowed (MEDIUM)

Same class as the blocked `user_words`/`user_patterns`. Traced `options.tesseract.config` → `_exec/tesseract.py:366,447` → `args_tesseract.extend(tessconfig)`: appended verbatim to the tesseract argv. Not shell injection — no shell is involved — but arbitrary argv injection, and `+` yields multiple tokens: `--tesseract-config /tmp/a+/tmp/b` → `['/tmp/a', '/tmp/b']`. Verified.

**Fix:** moved into `BLOCKED_PARAMETERS`.

### PR-4 — Language regex used `re.match` with `$` (LOW, but reachable)

`$` also matches before a trailing newline. Verified reachable: `--language eng\n+deu` → `{'language': ['eng\n', 'deu']}` **passed validation**. Low impact (argv, not shell), but it defeated the regex's stated purpose.

**Fix:** `re.fullmatch`.

### PR-5 — New log-injection sites (LOW)

The new validation path logged the caller-controlled key with f-strings — `logger.warning(f"Rejected unknown OCR parameter '{key}'")` — a fresh instance of SEC-7. A key containing CR/LF forges log entries.

**Fix:** `%r` lazy formatting, which escapes control characters.

---

## Still open — security

### SEC-2 — Resource guards remain caller-overridable (HIGH, partially closed)

`keep_temporary_files` is now blocked. The rest are not. Verified against the current branch:

```
--max-image-mpixels 100000  ->  accepted   # decompression-bomb guard effectively disabled
--jobs 10000                ->  accepted   # unbounded worker fan-out
```

The allow-list validates *names*. It does not validate *values*. A small crafted PDF plus a large `--max-image-mpixels` still exhausts container memory. This is the top remaining item.

### SEC-3 — No size limits anywhere; peak memory is a multiple of the document (HIGH)

`ocrservice.py`, `app.py`. The pipeline is in-memory and copies repeatedly: Starlette spools the upload, ocrmypdf writes the output into a `BytesIO`, `b64encode` copies at +33%, `.decode()` copies again, pydantic serialises a third time into the JSON response. Peak resident memory is roughly 4–5× the output document, and there is no maximum upload size at the app, at uvicorn, or in the ExApp deployment.

Compounding it: `ocrmypdf.api` holds a process-global `threading.Lock` around the whole pipeline run, so requests already serialise to one at a time — but nothing *rejects* the queued ones. They accumulate, each holding its uploaded bytes.

### SEC-4 — Blocking CPU work on the event loop stalls the process, including `/heartbeat` (HIGH)

`app.py` — `process_ocr` is `async def` but its body is entirely blocking. ocrmypdf is synchronous, CPU-bound, and can run for minutes. Declaring it `async` runs it *on the event loop*, so for the duration of an OCR run the process serves nothing else. `nc_py_api` registers `/heartbeat`, AppAPI polls it, and a stalled poll makes AppAPI conclude the ExApp is dead.

Note the inversion that suggests oversight rather than intent: `installed_languages` *is* declared `def`, so FastAPI offloads it to the threadpool. The cheap endpoint is offloaded; the expensive one is not.

### SEC-6 — Internal exception detail returned to the caller (MEDIUM)

`app.py` — the catch-all handler returns `f"{str(exc)} ({exc.__class__.__name__})"` for *every* unhandled exception. Exception strings routinely carry absolute temp paths, library internals, and fragments of input.

The `ExitCodeException` and `InvalidOcrParameterError` handlers are different cases and should stay as they are — the first is a contract the PHP client depends on (`message` + `ocrMyPdfExitCode`), and the second returns an app-authored message. Only the generic handler needs to become a fixed string plus a correlation id.

### SEC-7 — Unsanitised filename in logs and in the response (MEDIUM, partially closed)

The validation-path log injection introduced by #12 is fixed (PR-5). The original instance is not: `file.filename` is fully attacker-controlled and is still interpolated into a `logger.debug` f-string and echoed back verbatim as `OcrResult.filename`. Needs `os.path.basename`, control-character stripping, a length cap, and structured logging.

### SEC-8 — `/docs` and `/openapi.json` are unauthenticated (MEDIUM)

`AppAPIAuthMiddleware(disable_for=["docs", "openapi.json"])`. The middleware matches with `fnmatch` on the stripped path, so the exemption is exactly those two — no wildcard hazard — but both serve without authentication on the ExApp port. Gate them behind an env flag, default off in production.

### SEC-9 — Supply chain and release integrity (MEDIUM)

- **`<image-tag>master</image-tag>`** — every installation pulls a *mutable* tag. There is no way to pin, audit, or roll back a deployed version.
- **No transitive pinning** — direct deps are pinned exactly; everything underneath floats.
- **Actions pinned by tag, not SHA** — in workflows holding `APPSTORE_TOKEN` and `APP_PRIVATE_KEY`.
- **No `permissions:` block** in any workflow.
- Base image not digest-pinned; no Dependabot, CodeQL, or container scanning.

---

## Still open — correctness

All reproduced against the current branch.

| ID | Issue | Evidence |
|---|---|---|
| BUG-1 | Multi-token values silently truncated to the first token. **#12 made this worse**: it used to mangle silently, now it hard-fails | `--title Hello World` → `{'title': 'Hello'}`; `--clean --unpaper-args --layout single` → `400 Unknown parameter 'layout'` |
| BUG-2 | `str.isnumeric()` is true for Unicode numerics, then `int()` raises → unhandled 500 | `--oversample ²` → `ValueError: invalid literal for int()` |
| BUG-3 | Negative numbers never coerced; `--` inside a value corrupts the parse | `--skip-big -1` → `'-1'` (string); `--pages 1--2` → `{'pages': 1, '2': True}` |
| BUG-4 | Any value containing `+` becomes a list, even where a scalar is expected | `--title a+b` → `['a', 'b']` |
| BUG-5 | Duplicate keys silently overwrite instead of erroring | `--language eng --language deu` → `'deu'` |
| BUG-8 | `installed_languages` has no `check=` and no `timeout=`; a tesseract failure returns `[]`, indistinguishable from "no languages installed"; the `[1:]` header-skip is brittle | `ocrservice.py` |
| BUG-9 | `UploadFile.filename` is `str \| None`; a part without a filename → pydantic ValidationError → 500 | `OcrResult.filename: str` |
| BUG-10 | `sidecar_buffer.getvalue().decode("utf-8")` can raise `UnicodeDecodeError` → 500 | `ocrservice.py` |
| BUG-11 | Annotations claim `str` where `None` is the documented default | `app.py`, `ocrservice.py` |
| BUG-12 | `output_buffer.close()` called twice | harmless for `BytesIO`, but untidy |

Every one of BUG-1 through BUG-5 has the same root cause: `_split_parameters` still tokenises with `str.split("--")` and `str.split(" ")`. `shlex.split` fixes the class.

---

## Still open — best practices

| ID | Observation |
|---|---|
| BP-1 | `main.py` hardcodes `log_level="trace"`, activating uvicorn's `MessageLoggerMiddleware` — one log entry per ASGI message per request. *Checked:* it replaces headers and bodies with placeholders, so this is **not** a credential leak; it is log volume and disk pressure. Make it env-driven, default `info`. |
| BP-2 | `logging.getLogger('uvicorn.error')` couples application code to the server; logs vanish silently under any other runner. |
| BP-3 | No `__init__.py` in either package directory — implicit namespace packages. |
| BP-4 | No linter, formatter or type checker. BUG-9 and BUG-11 are exactly what `mypy` reports for free. |
| BP-5 | Largely addressed by #12, which added `test_ocrservice.py`. Still missing: a test asserting unauthenticated requests are rejected. |
| BP-6 | `.env` committed with `APP_SECRET=secret` and `APP_HOST=0.0.0.0`, loaded with `override=True` at test-import time and copied into the test image. Ship `.env.example`. |
| BP-7 | Dockerfile: `apk update` redundant alongside `--no-cache`; `apk search tesseract-ocr-data-` installs *every* language pack, making the image large and non-reproducible; no `--no-cache-dir`; no `HEALTHCHECK`; base image not digest-pinned. |
| BP-8 | `start.sh`: `set -e` without `-u`/`pipefail`; env vars interpolated into TOML unquoted and unvalidated; `frpc` backgrounded with no supervision; `echo "... $@"` should be `$*`. |
| BP-9 | `ErrorResult` is declared and referenced in `responses={...}` but never used to *build* a response — all three handlers hand-roll dicts, so the model and the wire format can drift. |
| BP-10 | `test.yml` builds and runs repository code in a job where HaRP receives `/var/run/docker.sock`. Contained on ephemeral GitHub-hosted runners; a critical escape the day it moves to a self-hosted runner. |
| BP-11 | `info.xml` carries `<version>1.35.0-dev</version>` on `master`. |
| BP-12 | No `SECURITY.md` or disclosure policy. |

---

## Revised plan

The original P0 was "replace `_split_parameters` with a validating allow-list parser". PR #12 plus this branch have done the **allow-list** half. The **validating** half is not done: names are checked, values are not.

### P0 — Validate values, not just names

**Closes:** SEC-2, BUG-1 … BUG-5.

The allow-list stops `--plugins`. It does nothing about `--max-image-mpixels 100000`, `--jobs 10000`, or `--optimize high` (which still reaches ocrmypdf and surfaces as a 500 from pydantic rather than a 400).

1. Give each allow-listed parameter a type and, where it governs resource use, a **bound**:
   `jobs` ≤ CPU budget, `max_image_mpixels` in `[1, 500]` — never 0 — `optimize` in `{0,1,2,3}`, `tesseract_timeout` ≤ a ceiling, enumerations checked against their choices.
2. **Tokenise with `shlex.split()`** instead of `split("--")` / `split(" ")`. One change closes BUG-1 through BUG-5 and makes quoting work.
3. Reject duplicates rather than silently overwriting.

### P1 — Put a ceiling on every resource

**Closes:** SEC-3, SEC-4, BUG-8.

1. `def process_ocr` instead of `async def`, so FastAPI runs it in the threadpool. One keyword; it is the difference between "slow" and "AppAPI restarts the container".
2. Enforce a maximum upload size from `Content-Length` before touching the body; stream to a `NamedTemporaryFile` and give ocrmypdf a path.
3. Bound concurrency with an `asyncio.Semaphore`, returning `503` when saturated.
4. Wall-clock timeout on the OCR run, and a default `tesseract_timeout`.
5. `timeout=` and `check=True` on the `installed_languages` subprocess; cache the result.

### P2 — Tighten the response and logging boundary

**Closes:** SEC-6, SEC-7, BUG-9 … BUG-12, BP-2, BP-9.

Generic handler returns a fixed message plus a correlation id; keep the `ExitCodeException` and `InvalidOcrParameterError` contracts and build all three through `ErrorResult`. Sanitise `file.filename`. Structured logging throughout. Fix the `str | None` annotations.

### P3 — Supply chain and release integrity

**Closes:** SEC-9, BP-7, BP-10.

Publish immutable image tags — the highest-value item here, since today there is no such thing as "the version I have installed". Hash-pinned lock file. SHA-pinned actions. Least-privilege `permissions:`. Digest-pinned base image. Dependabot, CodeQL, container scanning.

### P4 — Tooling and hygiene

**Closes:** BP-1, BP-3, BP-4, BP-5, BP-6, BP-8, BP-11, BP-12.

Env-driven `log_level`. `ruff` + `mypy` in CI. `.env.example`. `start.sh` hardening. `SECURITY.md`.

---

## What's already good

- PR #12's plugin test asserts the exploit *marker file* is never created, not merely that a 400 came back. That is how a code-execution fix should be tested.
- The layering is clean — the FastAPI module has no OCR logic and `OcrService` has no HTTP concerns.
- gosu is version-pinned *and* GPG signature-verified.
- The multi-stage Dockerfile keeps the passwordless-sudo `devcontainer` and `test` stages out of the published `app` image.
- The HaRP integration test stands up a real HaRP container, drives the real ExApp lifecycle, asserts on the generated `frpc.toml`, and cleans up in a `finally`.
- Direct dependencies are pinned exactly, and CI runs the tests inside the image that ships.
