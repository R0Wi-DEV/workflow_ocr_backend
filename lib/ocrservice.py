
import base64
from datetime import datetime, timezone
import io
from logging import Logger
from typing import Iterable
import ocrmypdf

from model.fileinfo import FileInfo
from model.ocrresult import OcrResult
import subprocess

class OcrService:
    def __init__(self, logger: Logger):
        self.logger = logger

    def ocr(self, file_info: FileInfo, ocrmypdf_parameters: str) -> OcrResult:
        output_buffer = io.BytesIO() 
        sidecar_buffer = io.BytesIO()
    
        try:
            current_time = datetime.now(timezone.utc).isoformat()
            self.logger.debug(f"{current_time} - Start processing file {file_info.filename} (OCR parameters: {ocrmypdf_parameters})")

            kwargs = self._split_parameters(ocrmypdf_parameters)
            exit_code = ocrmypdf.ocr(file_info.file, output_buffer, sidecar=sidecar_buffer, progress_bar=False, **kwargs)

            if exit_code != 0:
                raise Exception(f"ocr failed ({exit_code})")
            
            file_base64 = base64.b64encode(output_buffer.getvalue()).decode("utf-8")
            output_buffer.close()

            sidecar_text = sidecar_buffer.getvalue().decode("utf-8")
            sidecar_buffer.close()

            current_time = datetime.now(timezone.utc).isoformat()
            self.logger.debug(f"{current_time} - Finished processing file {file_info.filename}")

            return OcrResult(filename=file_info.filename, content_type="application/pdf", recognized_text=sidecar_text, file_content=file_base64)
        
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
            key = splitted_param[0]
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

            params[key] = value
        return params
