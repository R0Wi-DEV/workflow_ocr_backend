from pydantic import BaseModel, Field

class ErrorResult(BaseModel):
    message: str = Field(description='Error message')

class OcrResult(BaseModel):
    filename: str = Field(description='Name of the file')
    content_type: str = Field(serialization_alias='contentType', description='Content type of the file. For example: application/pdf')
    recognized_text: str = Field(serialization_alias='recognizedText', description='Recognized text from the file')
    file_content: str = Field(serialization_alias='fileContent', description='Base64 encoded file content')