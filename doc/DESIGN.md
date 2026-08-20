# Redesigning the `workflow_ocr_backend` OCR API

## The bug class, not the bug

[`doc/CODE_REVIEW.md`](CODE_REVIEW.md) closes the reachable RCE (`--plugins /tmp/evil.py`) and
tightens the parameter allow-list to CLI names. But the shape that produced the bug survived that
fix. The endpoint's contract was, effectively:

> Send me a string. I will parse it into keyword arguments and splat them into a third-party
> function.

```python
kwargs = self._split_parameters(ocrmypdf_parameters)
exit_code = ocrmypdf.ocr(file, output_buffer, sidecar=..., **kwargs)
```

Three compounding properties made this a recurring RCE generator rather than a one-off mistake:

1. **The sink is unbounded.** `ocrmypdf.ocr()` ends in `**kwargs`, and unknown keys are forwarded
   to `create_options`. It loads plugins and shells out to tesseract, ghostscript, unpaper,
   pngquant and jbig2enc. Its parameter list was never designed to be a trust boundary.
2. **The transport was untyped.** A CLI-ish string parsed by `split("--")` → `split(" ")` →
   shape-guessed types. Multi-token values were silently truncated, `+` turned any value into a
   list, and a value containing `--` corrupted the whole parse.
3. **An allow-list only checks names, never values.** `--max-image-mpixels 100000` or
   `--jobs 10000` still reached OCRmyPDF: the decompression-bomb guard and the worker-count knob
   were request fields, not operator policy.

## The redesign

**The API exposes a small closed vocabulary of OCR intents. It does not expose the dependency's
function signature, and resource limits are not caller options.**

### 1. Typed options, not a flag string

`POST /v1/ocr`, multipart: `file` plus an `options` part of `application/json` validated by a
hand-written Pydantic model with `extra="forbid"`
([`workflow_ocr_backend/ocroptions.py`](../workflow_ocr_backend/ocroptions.py)). Unknown key → 422
with a field path, never a silent forward. FastAPI generates the OpenAPI schema from the model, so
callers get a real contract instead of a doc link to the OCRmyPDF cookbook.

### 2. Constraints live in the types

Every scalar is bounded (`optimize: 0–3`, `tesseract_pagesegmode: 0–13`), every enumerated value is
a real enum, every string is a regex-constrained alias (`LanguageCode`, `PageRange`). Invalid states
are unrepresentable rather than rejected at runtime: `skip_text`/`force_ocr`/`redo_ocr` - three
booleans the legacy API let you set simultaneously, producing a 500 from OCRmyPDF's own validation
- collapse into one `TextMode` enum (`None` is a deliberate fourth state: "no override", matching
OCRmyPDF's own conservative default of refusing to touch a document that already has text).

### 3. Caller intent vs. operator policy

The split the legacy design lacked entirely. `jobs`, `max_image_mpixels` and the
`tesseract_timeout` ceiling live in `OcrPolicy`, built from environment variables
(`OCR_JOBS`, `OCR_MAX_IMAGE_MPIXELS`, `OCR_MAX_TESSERACT_TIMEOUT_S`) once at startup. A
caller-supplied timeout is *clamped*, never honoured upward
(`test_caller_cannot_raise_the_timeout_ceiling`). A test asserts that no request field can
influence any operator-owned kwarg (`test_operator_owned_kwargs_come_only_from_policy`).

### 4. Explicit mapping, no reflection

`OcrOptions.to_ocrmypdf_kwargs()` is written out field by field. No `**caller_data` anywhere.
Adding an option is a deliberate edit in three places (the field, the mapping, the frozen
`EMITTABLE_OCRMYPDF_KWARGS` set). This also decouples the public vocabulary from upstream's
representation: the API says `"sauvola"`, the mapping converts it to the `IntEnum` value `2`
OCRmyPDF actually wants; the public field is named `jpeg_quality` after the documented CLI flag,
and mapped to the `jpg_quality` keyword OCRmyPDF's Python signature actually exposes.

### 5. Two structural invariants, enforced in CI

- **No path-typed field.** `test_no_path_typed_fields` walks `OcrOptions.model_fields` and fails
  on any `Path`/`PathLike`/`FilePath` annotation. `--plugins` and `--user-words` were both "a path
  in the request body"; ban the shape, not the instances.
- **Signature drift breaks the build.** [`test/ocrmypdf_signature.json`](../test/ocrmypdf_signature.json)
  snapshots upstream's keyword-only parameters; `test_ocrmypdf_signature_has_not_drifted` diffs
  against it. This is the exact inverse of a derived allow-list: an OCRmyPDF upgrade that adds a
  parameter *fails CI* and someone has to review it and update the snapshot deliberately, instead
  of the boundary silently widening on `pip upgrade`.

### 6. Language validation against reality

`OcrOptions.validate_against_policy()` intersects requested languages with
`OcrPolicy.installed_languages`, cached from `tesseract --list-langs` at startup. Turns "language
not installed" from an OCRmyPDF-side 500 into an application-level 400.

## Migration: the legacy shim

Since the old contract can break callers, `/process_ocr` stays as a thin deprecated shim
([`workflow_ocr_backend/legacy.py`](../workflow_ocr_backend/legacy.py)) rather than being removed:
the flag string is tokenised (`shlex.split`, fixing the silent-truncation and corrupted-parse bugs
in the old tokenizer) and translated field-by-field onto `OcrOptions` through an explicit table.
Anything not in that table - `--plugins`, `--tesseract-config`, `--unpaper-args`, any
operator-owned knob - becomes a 400 by construction, because no code path ever puts it on the
model. Responses carry `Deprecation`/`Sunset`/`Link` headers pointing at `/v1/ocr`.

## Beyond the API surface

Two items from the schema's own blast radius were fixed alongside it:

- **`process_ocr` was `async def` but called blocking `ocrmypdf.ocr()`**, stalling the event loop -
  and AppAPI's `/heartbeat` poll - for the duration of every OCR run. Both `/v1/ocr` and
  `/process_ocr` are now plain `def`, so FastAPI runs them in the threadpool.
- **The generic exception handler echoed `str(exc)` at 500**, which routinely carries absolute
  temp paths and library internals. It now logs the detail server-side against a correlation id and
  returns only `"Internal server error [<id>]"`. The `ExitCodeException` handler is unchanged - its
  message is a deliberate part of the API contract, not a leak.

Everything else flagged in `doc/CODE_REVIEW.md` under P1–P4 (upload size limits, concurrency
bounds, a sandboxed worker process for the OCRmyPDF subprocess fan-out, filename sanitisation,
supply-chain pinning) is still open and tracked there; this redesign is scoped to the request
schema and the two items above.
