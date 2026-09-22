from typing import Any

from pydantic import BaseModel, Field

class ErrorResult(BaseModel):
    message: str = Field(description='Error message')
    ocr_my_pdf_exit_code: int | None = Field(default=None, serialization_alias='ocrMyPdfExitCode', description='Exit code of the OCRmyPDF process (if applicable)')

class ValidationErrorResult(BaseModel):
    message: str = Field(description='Error message')
    errors: list[dict[str, Any]] = Field(description='Field-level validation errors, as returned by pydantic')

class OcrResult(BaseModel):
    filename: str = Field(description='Name of the file')
    content_type: str = Field(serialization_alias='contentType', description='Content type of the file. For example: application/pdf')
    recognized_text: str = Field(serialization_alias='recognizedText', description='Recognized text from the file')
    file_content: str = Field(serialization_alias='fileContent', description='Base64 encoded file content')