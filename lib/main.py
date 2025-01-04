from contextlib import asynccontextmanager
from typing import Dict, Iterable

from fastapi import FastAPI, File, Form, UploadFile, Request

from fastapi.responses import JSONResponse
from nc_py_api import AsyncNextcloudApp, NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, run_app, set_handlers
import logging

from model.ocrresult import ErrorResult, OcrResult
from ocrservice import OcrService
from model.fileinfo import FileInfo

@asynccontextmanager
async def lifespan(app: FastAPI):
    set_handlers(app, enabled_handler)
    yield


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware, disable_for=["docs", "openapi.json"])
logger = logging.getLogger('uvicorn.error') # Use same logging as uvicorn


def enabled_handler(enabled: bool, _: NextcloudApp | AsyncNextcloudApp) -> str:
    # Nothing to do currently ...
    logger.debug(f"App enabled: {enabled}")
    return ""

@APP.exception_handler(Exception)
async def exception_handler(_: Request, exc: Exception):
    # Exception will be logged by uvicorn automatically.
    # It will also be turned into an ErrorResult response.
    return JSONResponse({"message": str(exc)}, status_code=500)


@APP.post("/process_ocr", response_model=OcrResult, responses={500: {"model": ErrorResult}})
async def process_ocr(
        file: UploadFile = File(..., description="The file to be processed using OCR."), 
        ocrmypdf_parameters: str = Form(None, description="Additional parameters for the OCRmyPdf process (see https://ocrmypdf.readthedocs.io/en/latest/cookbook.html#basic-examples).")
    ):
    """
    Processes an OCR request.
    This endpoint accepts a file upload and optional OCR parameters to process the file using OCR (Optical Character Recognition).
    """
    service = OcrService(logger)
    return service.ocr(FileInfo(file.file, file.filename), ocrmypdf_parameters)

@APP.get("/installed_languages", response_model=Iterable[str])
def installed_languages():
    """
    Retrieves the list of installed Tesseract languages - relevant for OCRmyPDF.
    """
    service = OcrService(logger)
    return service.installed_languages()

if __name__ == "__main__":
    # Note: host- and port-binding will be handled by NC library automatically
    run_app("main:APP", log_level="trace")