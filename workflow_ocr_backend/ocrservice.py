import base64
from datetime import datetime, timezone
import io
from logging import Logger
from typing import BinaryIO, Iterable
import ocrmypdf

from .legacy import options_from_legacy_parameters
from .model.ocrresult import OcrResult
from .ocroptions import OcrOptions, OcrPolicy
import subprocess


class OcrService:
    def __init__(self, logger: Logger):
        self.logger = logger

    def ocr(self, file: BinaryIO, file_name: str, options: OcrOptions, policy: OcrPolicy) -> OcrResult:
        """
        Runs OCRmyPDF for a validated, typed set of options.

        ``options`` is the only caller-controlled input handed to ocrmypdf. It was
        produced either by validating a JSON request body against ``OcrOptions``
        (the ``/v1/ocr`` endpoint) or by translating the deprecated flag-string
        format through ``legacy.options_from_legacy_parameters`` (the
        ``/process_ocr`` shim) - either way it already passed the schema's own
        (stateless) validation. The one check that depends on runtime state -
        whether the requested languages are installed - happens here, against
        ``policy``, so it can return 400 rather than surface as an OCRmyPDF 500.
        """
        output_buffer = io.BytesIO()
        sidecar_buffer = io.BytesIO()

        try:
            current_time = datetime.now(timezone.utc).isoformat()
            self.logger.debug(f"{current_time} - Start processing file {file_name} (OCR options: {options!r})")

            options.validate_against_policy(policy)
            kwargs = options.to_ocrmypdf_kwargs(policy)
            exit_code = ocrmypdf.ocr(file, output_buffer, sidecar=sidecar_buffer, **kwargs)

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

    def ocr_legacy(self, file: BinaryIO, file_name: str, ocrmypdf_parameters: str | None, policy: OcrPolicy) -> OcrResult:
        """
        Deprecated entry point for ``/process_ocr``. Translates the legacy
        ``--flag value`` string into ``OcrOptions`` (raising ``InvalidOcrParameterError``
        - a 400 - for anything the shim can't express) and delegates to ``ocr()``.
        """
        options = options_from_legacy_parameters(ocrmypdf_parameters)
        return self.ocr(file, file_name, options, policy)

    def installed_languages(self) -> Iterable[str]:
        # check=True + a timeout: this result seeds OcrPolicy at startup, so a
        # tesseract failure here must fail loudly rather than silently produce
        # an empty language set that then rejects every OCR request as 400.
        try:
            result = subprocess.run(
                ["tesseract", "--list-langs"], capture_output=True, text=True, check=True, timeout=30
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            self.logger.error(f"Could not determine installed tesseract languages: {exc}")
            raise
        languages = result.stdout.splitlines()[1:]  # Skip the first line
        return [lang for lang in languages if lang != "osd"]
