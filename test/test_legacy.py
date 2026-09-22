import pytest

from workflow_ocr_backend.legacy import InvalidOcrParameterError, options_from_legacy_parameters
from workflow_ocr_backend.ocroptions import TextMode


def test_options_from_legacy_parameters_none():
    options = options_from_legacy_parameters(None)
    assert options.languages == ["eng"]
    # No mode flag given -> no override, matching ocrmypdf's own conservative
    # default of erroring on an already-processed document.
    assert options.mode is None


def test_options_from_legacy_parameters_valid():
    options = options_from_legacy_parameters("--skip-text --tesseract-pagesegmode 7 --language eng+chi_sim")
    assert options.mode == TextMode.SKIP
    assert options.tesseract_pagesegmode == 7
    assert options.languages == ["eng", "chi_sim"]


def test_options_from_legacy_parameters_force_and_redo_ocr():
    assert options_from_legacy_parameters("--force-ocr").mode == TextMode.FORCE
    assert options_from_legacy_parameters("--redo-ocr").mode == TextMode.REDO


@pytest.mark.parametrize("parameters", [
    "--plugins /tmp/evil.py",
    "--plugin-manager foo",
    "--user-words /etc/passwd",
    "--user-patterns /etc/passwd",
    "--keep-temporary-files",
    "--sidecar /tmp/out.txt",
    "--output-file /tmp/out.pdf",
    # tesseract_config is appended verbatim to the tesseract argv, so a caller-supplied
    # value is an arbitrary config-file path in the same way user_words is.
    "--tesseract-config /tmp/evil.conf",
    "--unpaper-args --layout single",
])
def test_options_from_legacy_parameters_rejects_dangerous_parameters(parameters):
    # These parameters aren't in the translation table at all, so they can never
    # reach OcrOptions - the shim's allow-list is what it can express, not a
    # denylist of what it blocks.
    with pytest.raises(InvalidOcrParameterError):
        options_from_legacy_parameters(parameters)


@pytest.mark.parametrize("parameters", [
    "--not-an-ocrmypdf-parameter",
    "--some-unknown-option value",
    # Operator-owned resource knobs are not caller options at all anymore.
    "--jobs 10000",
    "--max-image-mpixels 100000",
])
def test_options_from_legacy_parameters_rejects_unknown_parameters(parameters):
    with pytest.raises(InvalidOcrParameterError):
        options_from_legacy_parameters(parameters)


@pytest.mark.parametrize("parameters", [
    "--language eng;id",
    "--language $(id)",
    "--language `id`",
    "--language |id",
    "--language eng+;id",
    "--language ../../etc/passwd",
    "--language -eng",
])
def test_options_from_legacy_parameters_rejects_invalid_languages(parameters):
    with pytest.raises(InvalidOcrParameterError, match="Invalid language value"):
        options_from_legacy_parameters(parameters)


@pytest.mark.parametrize("parameters,expected", [
    ("--language eng", ["eng"]),
    ("--language chi_sim", ["chi_sim"]),
    ("--language eng+deu", ["eng", "deu"]),
])
def test_options_from_legacy_parameters_accepts_valid_languages(parameters, expected):
    assert options_from_legacy_parameters(parameters).languages == expected


@pytest.mark.parametrize("parameters,expected", [
    ("--jpeg-quality 80", 80),
    ("--jpg-quality 80", 80),
])
def test_options_from_legacy_parameters_accepts_both_jpeg_quality_spellings(parameters, expected):
    # --jpeg-quality is the documented CLI flag; --jpg-quality is its hidden
    # argparse.SUPPRESS-ed alias. Both must land on the same field.
    assert options_from_legacy_parameters(parameters).jpeg_quality == expected


@pytest.mark.parametrize("parameters", ["--quiet", "--verbose", "--no-progress-bar"])
def test_options_from_legacy_parameters_drops_cli_only_flags(parameters):
    # CLI-only logging flags have no OCRmyPDF API equivalent. They are dropped
    # rather than rejected so existing workflow configurations keep working.
    options = options_from_legacy_parameters(f"{parameters} --language eng")
    assert options.languages == ["eng"]


def test_options_from_legacy_parameters_rejects_duplicate_flags():
    with pytest.raises(InvalidOcrParameterError, match="Duplicate"):
        options_from_legacy_parameters("--language eng --language deu")


def test_options_from_legacy_parameters_rejects_unquoted_multi_word_value():
    # Unquoted multi-word values used to be silently truncated to their first
    # token. They are now a 400 - the caller must quote the value.
    with pytest.raises(InvalidOcrParameterError):
        options_from_legacy_parameters("--title Hello World")


def test_options_from_legacy_parameters_accepts_quoted_multi_word_value():
    options = options_from_legacy_parameters('--title "Hello World"')
    assert options.title == "Hello World"
