import logging
import pytest

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
