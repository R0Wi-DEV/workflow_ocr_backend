import inspect
import logging
import pytest

import ocrmypdf
from ocrmypdf._options import OcrOptions

from workflow_ocr_backend.ocrservice import InvalidOcrParameterError, OcrService

service = OcrService(logging.getLogger(__name__))

def test_split_parameters_valid():
    params = service._split_parameters("--skip-text --tesseract-pagesegmode 7 --language eng+chi_sim")
    assert params == {"skip_text": True, "tesseract_pagesegmode": 7, "language": ["eng", "chi_sim"]}

def test_split_parameters_none():
    assert service._split_parameters(None) == {}

@pytest.mark.parametrize("parameters", [
    "--plugins /tmp/evil.py",
    "--plugin-manager foo",
    "--user-words /etc/passwd",
    "--user-patterns /etc/passwd",
    "--keep-temporary-files",
    "--sidecar /tmp/out.txt",
    "--output-file /tmp/out.pdf",
    "--progress-bar",
    # tesseract_config is appended verbatim to the tesseract argv, so a caller-supplied
    # value is an arbitrary config-file path in the same way user_words is.
    "--tesseract-config /tmp/evil.conf",
    "--tesseract-config /tmp/a+/tmp/b",
])
def test_split_parameters_rejects_blocked_parameters(parameters):
    # These parameters would allow the caller to execute arbitrary code (plugins),
    # access the backend's filesystem or overwrite values controlled by this service.
    with pytest.raises(InvalidOcrParameterError):
        service._split_parameters(parameters)

@pytest.mark.parametrize("parameters", [
    "--not-an-ocrmypdf-parameter",
    "--some-unknown-option value",
])
def test_split_parameters_rejects_unknown_parameters(parameters):
    with pytest.raises(InvalidOcrParameterError):
        service._split_parameters(parameters)

@pytest.mark.parametrize("parameters", [
    "--language eng;id",
    "--language $(id)",
    "--language `id`",
    "--language |id",
    "--language eng+;id",
    "--language ../../etc/passwd",
    "--language -eng",
    "--language 123",
    # '$' in a regex also matches before a trailing newline, so this passed while the
    # check used re.match instead of re.fullmatch.
    "--language eng\n+deu",
])
def test_split_parameters_rejects_invalid_languages(parameters):
    # Language values must match the allow-list pattern used by the Nextcloud app,
    # so nothing which could be (ab)used as a shell metacharacter is passed on.
    with pytest.raises(InvalidOcrParameterError):
        service._split_parameters(parameters)

@pytest.mark.parametrize("parameters,expected", [
    ("--language eng", "eng"),
    ("--language chi_sim", "chi_sim"),
    ("--language script/Latin", "script/Latin"),
    ("--language eng+deu+script/Latin", ["eng", "deu", "script/Latin"]),
])
def test_split_parameters_accepts_valid_languages(parameters, expected):
    assert service._split_parameters(parameters) == {"language": expected}

@pytest.mark.parametrize("parameters,expected", [
    # --ocr-engine is a documented CLI flag and a real OcrOptions field, but it is not a
    # keyword argument of ocrmypdf.ocr(), so a signature-derived allow-list rejects it.
    ("--ocr-engine none", {"ocr_engine": "none"}),
    # --jpeg-quality is the documented spelling; --jpg-quality is the hidden alias.
    # Both must be accepted, and both must arrive as the keyword ocrmypdf.ocr() takes.
    ("--jpeg-quality 80", {"jpg_quality": 80}),
    ("--jpg-quality 80", {"jpg_quality": 80}),
])
def test_split_parameters_accepts_documented_cli_names(parameters, expected):
    assert service._split_parameters(parameters) == expected

@pytest.mark.parametrize("parameters,expected", [
    ("--quiet", {}),
    ("--verbose", {}),
    ("--quiet --language eng", {"language": "eng"}),
])
def test_split_parameters_drops_cli_only_flags(parameters, expected):
    # CLI-only logging flags have no OCRmyPDF API equivalent. They are dropped rather than
    # rejected so that existing workflow configurations carrying them keep working.
    assert service._split_parameters(parameters) == expected

def test_allowed_parameters_still_resolve_against_installed_ocrmypdf():
    # The allow-list is an explicit literal, so an OCRmyPDF upgrade cannot silently widen
    # it. This guard catches the opposite risk: an upgrade renaming or removing an option
    # would otherwise leave a dead entry that 400s at runtime with no test failure.
    ocr_keywords = {
        name for name, param in inspect.signature(ocrmypdf.ocr).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }
    option_fields = set(OcrOptions.model_fields.keys())
    unresolved = sorted(
        name for name in OcrService.ALLOWED_PARAMETERS
        if OcrService.PARAMETER_ALIASES.get(name, name) not in ocr_keywords | option_fields
    )
    assert not unresolved, (
        f"Allow-listed parameters no longer accepted by ocrmypdf {ocrmypdf.__version__}: "
        f"{unresolved}. Check whether they were renamed or removed."
    )

def test_blocked_and_allowed_parameters_are_disjoint():
    assert not (OcrService.ALLOWED_PARAMETERS & OcrService.BLOCKED_PARAMETERS)
    assert not (OcrService.ALLOWED_PARAMETERS & OcrService.IGNORED_PARAMETERS)
