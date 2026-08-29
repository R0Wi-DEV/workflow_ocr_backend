"""
These tests are the load-bearing part of the design.

The schema alone stops today's RCE. These tests stop tomorrow's, by making the
trust boundary something that *fails CI when it moves* rather than something
that silently follows a dependency.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from workflow_ocr_backend.ocroptions import (
    EMITTABLE_OCRMYPDF_KWARGS,
    NEVER_EMITTED,
    OPERATOR_OWNED,
    OcrOptions,
    OcrPolicy,
    TextMode,
)

POLICY = OcrPolicy(installed_languages=frozenset({"eng", "deu", "chi_sim", "script/Latin"}))


# ---------------------------------------------------------------------------
# Invariant 1: the mapping can only ever produce keys from the frozen literal.
# ---------------------------------------------------------------------------


def test_mapping_never_emits_outside_the_frozen_set():
    """Exhaustive over the schema: set every field, check nothing new appears."""
    maximal = OcrOptions(
        languages=["eng", "deu"],
        mode=TextMode.FORCE,
        pages="1-4,7",
        rotate_pages=True,
        rotate_pages_threshold=12.0,
        deskew=True,
        clean=True,
        clean_final=True,
        remove_background=True,
        remove_vectors=True,
        oversample_dpi=400,
        image_dpi=300,
        optimize=3,
        jpeg_quality=80,
        png_quality=80,
        tesseract_pagesegmode=7,
        tesseract_oem=1,
        tesseract_thresholding="sauvola",
        tesseract_timeout_s=60.0,
        title="t",
        author="a",
        subject="s",
        keywords="k",
    )
    emitted = set(maximal.to_ocrmypdf_kwargs(POLICY))
    assert emitted <= EMITTABLE_OCRMYPDF_KWARGS, emitted - EMITTABLE_OCRMYPDF_KWARGS


@pytest.mark.parametrize("forbidden", sorted(NEVER_EMITTED))
def test_forbidden_kwargs_are_not_emittable(forbidden):
    assert forbidden not in EMITTABLE_OCRMYPDF_KWARGS, NEVER_EMITTED[forbidden]


def test_operator_owned_kwargs_come_only_from_policy():
    """Two very different requests must produce identical operator-owned values."""
    a = OcrOptions().to_ocrmypdf_kwargs(POLICY)
    b = OcrOptions(
        languages=["deu"], mode=TextMode.REDO, optimize=3, tesseract_timeout_s=3600.0
    ).to_ocrmypdf_kwargs(POLICY)
    for key in OPERATOR_OWNED:
        assert a[key] == b[key], f"caller influenced operator-owned kwarg {key!r}"


def test_caller_cannot_raise_the_timeout_ceiling():
    policy = OcrPolicy(max_tesseract_timeout_s=30.0, installed_languages=frozenset({"eng"}))
    kwargs = OcrOptions(tesseract_timeout_s=3600.0).to_ocrmypdf_kwargs(policy)
    assert kwargs["tesseract_timeout"] == 30.0


def test_jpeg_quality_field_maps_to_the_kwarg_ocrmypdf_actually_takes():
    """Public field is named after the documented CLI flag; the Python keyword differs."""
    kwargs = OcrOptions(jpeg_quality=80).to_ocrmypdf_kwargs(POLICY)
    assert kwargs["jpg_quality"] == 80
    assert "jpeg_quality" not in kwargs


# ---------------------------------------------------------------------------
# Invariant 2: no field in the schema can carry a path or free-form argv.
# ---------------------------------------------------------------------------


def test_no_path_typed_fields():
    """A path-typed field is how --plugins and --user-words got in. Ban the shape."""
    banned = {"Path", "PurePath", "PathLike", "FilePath", "DirectoryPath", "AnyUrl"}
    for name, field in OcrOptions.model_fields.items():
        rendered = str(field.annotation)
        assert not (banned & set(rendered.replace("'", " ").split())), (
            f"field {name!r} is path-typed: {rendered}"
        )


def test_unknown_fields_are_rejected_not_forwarded():
    with pytest.raises(ValidationError):
        OcrOptions(plugins="/tmp/evil.py")
    with pytest.raises(ValidationError):
        OcrOptions(tesseract_config=["/etc/passwd"])
    with pytest.raises(ValidationError):
        OcrOptions(unpaper_args="--layout single")


@pytest.mark.parametrize(
    "language",
    ["eng;id", "$(id)", "`id`", "|id", "../../etc/passwd", "-eng", "123", "eng+deu", "e" * 64],
)
def test_language_allowlist(language):
    with pytest.raises(ValidationError):
        OcrOptions(languages=[language])


def test_unavailable_language_is_a_client_error():
    opts = OcrOptions(languages=["jpn"])  # syntactically fine
    with pytest.raises(ValueError, match="not installed"):
        opts.validate_against_policy(POLICY)


@pytest.mark.parametrize("pages", ["1;id", "$(id)", "1-4 7", "../1", "a-b"])
def test_page_range_allowlist(pages):
    with pytest.raises(ValidationError):
        OcrOptions(pages=pages)


def test_mutually_exclusive_modes_are_unrepresentable():
    """The legacy API accepted skip_text and force_ocr together. This one can't."""
    assert set(TextMode) == {TextMode.SKIP, TextMode.FORCE, TextMode.REDO}
    kwargs = OcrOptions(mode=TextMode.FORCE).to_ocrmypdf_kwargs(POLICY)
    assert kwargs["force_ocr"] is True
    assert "skip_text" not in kwargs and "redo_ocr" not in kwargs


# ---------------------------------------------------------------------------
# Invariant 3: upstream drift is a human decision, not a silent widening.
# ---------------------------------------------------------------------------

SNAPSHOT = Path(__file__).parent / "ocrmypdf_signature.json"


def _current_signature() -> list[str]:
    import inspect

    import ocrmypdf

    return sorted(
        name
        for name, p in inspect.signature(ocrmypdf.ocr).parameters.items()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    )


def test_ocrmypdf_signature_has_not_drifted():
    """
    Fails when an ocrmypdf upgrade adds or removes a keyword parameter.

    This is the inverse of deriving an allowlist from inspect.signature(). A
    derived allowlist grows automatically on `pip upgrade` - a new upstream
    option becomes reachable from the internet with no code change and no
    review. Here the upgrade breaks the build instead, and someone has to look
    at the new parameter and decide whether it belongs in the schema.
    """
    expected = json.loads(SNAPSHOT.read_text())["keyword_only_parameters"]
    actual = _current_signature()
    added, removed = sorted(set(actual) - set(expected)), sorted(set(expected) - set(actual))
    assert not (added or removed), (
        f"ocrmypdf.ocr() signature changed. Added: {added}. Removed: {removed}. "
        "Review each new parameter for path/plugin/argv semantics, then update "
        "the snapshot deliberately."
    )


def test_every_emittable_kwarg_still_exists_upstream():
    """Catches the opposite failure: we emit a kwarg upstream has dropped."""
    upstream = set(_current_signature())
    stale = EMITTABLE_OCRMYPDF_KWARGS - upstream
    assert not stale, f"emitting kwargs ocrmypdf no longer accepts: {sorted(stale)}"
