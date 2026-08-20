"""
Deprecated ``--flag value`` request format, translated into :class:`OcrOptions`.

This is the shim promised by the redesign: ``/process_ocr`` keeps accepting the
old ``ocrmypdf_parameters`` string, but it no longer builds a kwargs dict that
gets splatted into ``ocrmypdf.ocr()``. It tokenises the string, translates each
recognised flag into a field on :class:`workflow_ocr_backend.ocroptions.OcrOptions`
through an explicit table, and lets that schema's own validation - not a
denylist - decide what is accepted. Anything the table does not recognise
(``--plugins``, ``--tesseract-config``, ``--unpaper-args``, ...) is a 400 by
construction, because there is no line in the table that would ever place it on
the model.
"""

from __future__ import annotations

import shlex
from typing import Any, Final

from pydantic import ValidationError

from .ocroptions import NEVER_EMITTED, InvalidOcrOptionsError, OcrOptions, TextMode


class InvalidOcrParameterError(InvalidOcrOptionsError):
    """Raised when the caller sent a legacy OCR parameter which is not allowed."""


# Boolean "presence" flags that select OcrOptions.mode instead of a same-named
# field. Mutually exclusive by construction - the last one seen wins, matching
# the OcrOptions default of TextMode.SKIP when none are present.
_MODE_FLAGS: Final[dict[str, TextMode]] = {
    "skip_text": TextMode.SKIP,
    "force_ocr": TextMode.FORCE,
    "redo_ocr": TextMode.REDO,
}

# Legacy CLI option name (normalised: '-' -> '_') -> OcrOptions field name.
# Deliberately a literal, hand-maintained table: the set of names this shim can
# translate IS the allow-list. A name that isn't here can never reach OcrOptions,
# so it can never reach ocrmypdf, regardless of what future versions accept.
_FIELD_MAP: Final[dict[str, str]] = {
    "pages": "pages",
    "rotate_pages": "rotate_pages",
    "rotate_pages_threshold": "rotate_pages_threshold",
    "deskew": "deskew",
    "clean": "clean",
    "clean_final": "clean_final",
    "remove_background": "remove_background",
    "remove_vectors": "remove_vectors",
    "oversample": "oversample_dpi",
    "image_dpi": "image_dpi",
    "output_type": "output_type",
    "optimize": "optimize",
    "jpeg_quality": "jpeg_quality",
    "jpg_quality": "jpeg_quality",  # hidden ocrmypdf CLI alias, same target field
    "png_quality": "png_quality",
    "pdf_renderer": "pdf_renderer",
    "tesseract_pagesegmode": "tesseract_pagesegmode",
    "tesseract_oem": "tesseract_oem",
    "tesseract_thresholding": "tesseract_thresholding",
    "tesseract_timeout": "tesseract_timeout_s",
    "title": "title",
    "author": "author",
    "subject": "subject",
    "keywords": "keywords",
}

# CLI-only flags with no OCRmyPDF API equivalent. Accepted and dropped rather
# than rejected, so existing workflow configurations carrying them keep working.
_IGNORED_PARAMETERS: Final[frozenset[str]] = frozenset({"quiet", "verbose", "no_progress_bar"})


def _tokenize(ocrmypdf_parameters: str) -> dict[str, str | bool]:
    """
    Splits a ``--flag value --other-flag`` string into a raw {name: value} dict.

    Uses ``shlex.split`` rather than ``str.split("--")`` / ``str.split(" ")``, so
    a quoted value ("--title 'Hello World'") survives intact instead of being
    silently truncated to its first token, and a value containing "--" is not
    misread as a new flag.
    """
    try:
        tokens = shlex.split(ocrmypdf_parameters)
    except ValueError as exc:  # e.g. unbalanced quotes
        raise InvalidOcrParameterError(f"Could not parse OCR parameters: {exc}") from exc

    raw: dict[str, str | bool] = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            raise InvalidOcrParameterError(f"Expected a '--flag', got {token!r}")
        key = token[2:].strip().replace("-", "_")
        if not key:
            raise InvalidOcrParameterError("Empty parameter name")
        if key in raw:
            raise InvalidOcrParameterError(f"Duplicate parameter {key!r}")

        if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            raw[key] = tokens[i + 1]
            i += 2
        else:
            raw[key] = True
            i += 1
    return raw


def options_from_legacy_parameters(ocrmypdf_parameters: str | None) -> OcrOptions:
    """Translates the deprecated flag-string format into a validated OcrOptions."""
    if not ocrmypdf_parameters:
        return OcrOptions()

    raw = _tokenize(ocrmypdf_parameters)
    fields: dict[str, Any] = {}
    mode: TextMode | None = None

    for key, value in raw.items():
        if key in _IGNORED_PARAMETERS:
            continue

        if key == "language":
            languages = value.split("+") if isinstance(value, str) else [value]
            fields["languages"] = languages
            continue

        if key in _MODE_FLAGS:
            if value is True:
                mode = _MODE_FLAGS[key]
            continue

        if key not in _FIELD_MAP:
            if key in NEVER_EMITTED:
                # Known-dangerous parameter: name it explicitly, matching the
                # legacy API's own wording for this case.
                raise InvalidOcrParameterError(f"Parameter '{key}' is not allowed")
            raise InvalidOcrParameterError(f"Unknown parameter '{key}'")

        fields[_FIELD_MAP[key]] = value

    if mode is not None:
        fields["mode"] = mode

    try:
        return OcrOptions(**fields)
    except ValidationError as exc:
        raise InvalidOcrParameterError(_first_validation_message(exc)) from exc


def _first_validation_message(exc: ValidationError) -> str:
    """Renders a pydantic ValidationError as the single-line message the legacy
    API contract expects, e.g. "Invalid language value '$(id)'" for a bad
    language rather than pydantic's generic pattern-mismatch wording."""
    error = exc.errors()[0]
    field = ".".join(str(loc) for loc in error["loc"])
    if field.startswith("languages"):
        bad = error.get("input")
        return f"Invalid language value {bad!r}"
    return f"Invalid value for '{field}': {error['msg']}"
