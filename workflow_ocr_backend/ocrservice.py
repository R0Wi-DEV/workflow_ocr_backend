
import base64
from datetime import datetime, timezone
import inspect
import io
from logging import Logger
import re
from typing import BinaryIO, Iterable
import ocrmypdf

from .model.ocrresult import OcrResult
import subprocess

class InvalidOcrParameterError(ValueError):
    """Raised when the caller sent an OCRmyPDF parameter which is not allowed."""

class OcrService:
    # Allow-list for tesseract/OCRmyPDF language codes (e.g. 'eng', 'chi_sim', 'script/Latin').
    # Same pattern as the one used by the Nextcloud app (workflow_ocr) so that language values
    # which could be (ab)used as shell metacharacters never reach the OCR engine.
    LANGUAGE_CODE_REGEX = re.compile(r"^[A-Za-z][A-Za-z0-9_/]{0,31}$")

    # Parameters which must never be taken from a request, even though ocrmypdf.ocr() accepts them:
    # * plugins/plugin_manager load arbitrary Python code => remote code execution
    # * input/output/sidecar/progress_bar are controlled by this service
    # * user_words/user_patterns/keep_temporary_files give access to the backend's filesystem
    BLOCKED_PARAMETERS = frozenset({
        "plugins",
        "plugin_manager",
        "input_file",
        "input_file_or_options",
        "output_file",
        "output_folder",
        "sidecar",
        "progress_bar",
        "user_words",
        "user_patterns",
        "keep_temporary_files",
    })

    # Everything OCRmyPDF documents as a keyword argument of ocrmypdf.ocr(), minus the blocked ones.
    # Unknown parameters are rejected instead of being silently forwarded, so that neither typos nor
    # future (potentially dangerous) OCRmyPDF options can be smuggled in via the request.
    ALLOWED_PARAMETERS = frozenset(
        name for name, param in inspect.signature(ocrmypdf.ocr).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    ) - BLOCKED_PARAMETERS

    LANGUAGE_PARAMETERS = frozenset({"language"})

    def __init__(self, logger: Logger):
        self.logger = logger

    def ocr(self, file: BinaryIO, file_name: str, ocrmypdf_parameters: str) -> OcrResult:
        output_buffer = io.BytesIO() 
        sidecar_buffer = io.BytesIO()
    
        try:
            current_time = datetime.now(timezone.utc).isoformat()
            self.logger.debug(f"{current_time} - Start processing file {file_name} (OCR parameters: {ocrmypdf_parameters})")

            kwargs = self._split_parameters(ocrmypdf_parameters)
            exit_code = ocrmypdf.ocr(file, output_buffer, sidecar=sidecar_buffer, progress_bar=False, **kwargs)

            if exit_code != 0:
                raise Exception(f"ocr failed ({exit_code})")
            
            file_base64 = base64.b64encode(output_buffer.getvalue()).decode("utf-8")
            output_buffer.close()

            sidecar_text = sidecar_buffer.getvalue().decode("utf-8")
            sidecar_buffer.close()

            current_time = datetime.now(timezone.utc).isoformat()
            self.logger.debug(f"{current_time} - Finished processing file {file_name}")

            return OcrResult(filename=file_name, content_type="application/pdf", recognized_text=sidecar_text, file_content=file_base64)
        
        finally:
            output_buffer.close()
            sidecar_buffer.close()

    def installed_languages(self) -> Iterable[str]:
        result = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
        languages = result.stdout.splitlines()[1:]  # Skip the first line
        return [lang for lang in languages if lang != "osd"]

    def _split_parameters(self, ocrmypdf_parameters: str) -> dict[str, str | bool | Iterable[str] | int | float]:
        if ocrmypdf_parameters is None:
            return {}
        
        params = {}

        for param in [p.strip() for p in ocrmypdf_parameters.split("--")]:
            if not param:
                continue
            splitted_param = [p.strip() for p in param.split(" ")]
            key = splitted_param[0].replace("-", "_")
            length = len(splitted_param)
            if length >= 2:
                value = splitted_param[1]
                # Multiple values
                if "+" in value:
                    value = value.split("+")
                # Single value (might be of type str, bool, int or float)
                elif value.isnumeric():
                    value = int(value)
                elif value.replace(".", "", 1).isnumeric():
                    value = float(value)
                elif value.lower() == "true":
                    value = True
                elif value.lower() == "false":
                    value = False
            else:
                # Flag
                value = True

            self._check_parameter(key, value)

            params[key] = value
        return params

    def _check_parameter(self, key: str, value: str | bool | Iterable[str] | int | float) -> None:
        """
        Validates a single OCRmyPDF parameter before it's handed over to ocrmypdf.ocr().
        This is a security relevant check: the parameters are fully controlled by the caller
        and are used to invoke the OCR engine (which in turn spawns subprocesses), so only
        known-good parameters and language codes may pass.
        """
        if key in self.BLOCKED_PARAMETERS:
            self.logger.warning(f"Rejected blocked OCR parameter '{key}'")
            raise InvalidOcrParameterError(f"Parameter '{key}' is not allowed")

        if key not in self.ALLOWED_PARAMETERS:
            self.logger.warning(f"Rejected unknown OCR parameter '{key}'")
            raise InvalidOcrParameterError(f"Unknown parameter '{key}'")

        if key in self.LANGUAGE_PARAMETERS:
            languages = value if isinstance(value, list) else [value]
            for language in languages:
                if not isinstance(language, str) or not self.LANGUAGE_CODE_REGEX.match(language):
                    self.logger.warning(f"Rejected invalid OCR language value: {language!r}")
                    raise InvalidOcrParameterError(f"Invalid language value '{language}'")
