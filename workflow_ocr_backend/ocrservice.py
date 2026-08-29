
import base64
from datetime import datetime, timezone
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

    # Parameters which must never be taken from a request, even though OCRmyPDF accepts them:
    # * plugins/plugin_manager load arbitrary Python code => remote code execution
    # * input/output/sidecar/progress_bar are controlled by this service
    # * user_words/user_patterns/keep_temporary_files give access to the backend's filesystem
    # * tesseract_config is appended verbatim to the tesseract argv (see _exec/tesseract.py),
    #   so a caller-supplied value is an arbitrary config-file path just like user_words
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
        "tesseract_config",
    })

    # Allow-list of OCRmyPDF *CLI option* names (normalised: '-' replaced by '_'), because that
    # is what callers send. Deliberately an explicit literal instead of introspecting
    # ocrmypdf.ocr(): its Python keyword names differ from the documented CLI spellings
    # (e.g. --jpeg-quality vs jpg_quality, --ocr-engine is not a keyword argument at all),
    # and introspection would silently widen this set on every OCRmyPDF upgrade.
    # test_ocrservice.py asserts every entry still resolves against the installed OCRmyPDF.
    ALLOWED_PARAMETERS = frozenset({
        # Language and OCR engine selection
        "language", "ocr_engine", "mode", "force_ocr", "skip_text", "redo_ocr",
        "pages", "skip_big",
        # Image preprocessing
        "image_dpi", "oversample", "deskew", "clean", "clean_final", "unpaper_args",
        "remove_background", "remove_vectors", "rotate_pages", "rotate_pages_threshold",
        # Tesseract tuning
        "tesseract_oem", "tesseract_pagesegmode", "tesseract_thresholding",
        "tesseract_timeout", "tesseract_non_ocr_timeout",
        "tesseract_downsample_above", "tesseract_downsample_large_images",
        # Output and PDF generation
        "output_type", "pdf_renderer", "rasterizer", "pdfa_image_compression",
        "color_conversion_strategy", "tagged_pdf_mode", "fast_web_view", "no_overwrite",
        "invalidate_digital_signatures", "continue_on_soft_render_error",
        # Optimisation
        "optimize", "jpeg_quality", "jpg_quality", "png_quality",
        "jbig2_lossy", "jbig2_page_group_size", "jbig2_threshold",
        # Document metadata
        "title", "author", "subject", "keywords",
        # Resource usage
        "jobs", "use_threads", "max_image_mpixels",
    })

    # Documented CLI option name -> ocrmypdf.ocr() keyword argument, where the two differ.
    # --jpeg-quality is the documented flag; --jpg-quality is its hidden (argparse.SUPPRESS)
    # alias and the only spelling the Python signature exposes.
    PARAMETER_ALIASES = {
        "jpeg_quality": "jpg_quality",
    }

    # CLI-only flags with no OCRmyPDF API equivalent. Accepted and dropped rather than
    # rejected, so existing workflow configurations carrying them keep working.
    IGNORED_PARAMETERS = frozenset({"quiet", "verbose", "no_progress_bar"})

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

            if key in self.IGNORED_PARAMETERS:
                self.logger.debug("Ignoring CLI-only OCR parameter %r", key)
                continue

            self._check_parameter(key, value)

            params[self.PARAMETER_ALIASES.get(key, key)] = value
        return params

    def _check_parameter(self, key: str, value: str | bool | Iterable[str] | int | float) -> None:
        """
        Validates a single OCRmyPDF parameter before it's handed over to ocrmypdf.ocr().
        This is a security relevant check: the parameters are fully controlled by the caller
        and are used to invoke the OCR engine (which in turn spawns subprocesses), so only
        known-good parameters and language codes may pass.
        """
        # Note: %r rather than an f-string, so control characters in the caller-supplied
        # key are escaped instead of forging additional log lines.
        if key in self.BLOCKED_PARAMETERS:
            self.logger.warning("Rejected blocked OCR parameter %r", key)
            raise InvalidOcrParameterError(f"Parameter '{key}' is not allowed")

        if key not in self.ALLOWED_PARAMETERS:
            self.logger.warning("Rejected unknown OCR parameter %r", key)
            raise InvalidOcrParameterError(f"Unknown parameter '{key}'")

        if key in self.LANGUAGE_PARAMETERS:
            languages = value if isinstance(value, list) else [value]
            for language in languages:
                # fullmatch, not match: '$' would also match before a trailing newline,
                # so re.match would accept 'eng\n' (reachable via '--language eng\n+deu').
                if not isinstance(language, str) or not self.LANGUAGE_CODE_REGEX.fullmatch(language):
                    self.logger.warning("Rejected invalid OCR language value: %r", language)
                    raise InvalidOcrParameterError(f"Invalid language value '{language}'")
