from contextlib import asynccontextmanager
from typing import Iterable

from fastapi import FastAPI, File, Form, UploadFile, Request

from fastapi.responses import JSONResponse
from nc_py_api import AsyncNextcloudApp, NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, set_handlers
import logging

from ocrmypdf import ExitCodeException

from .model.ocrresult import ErrorResult, OcrResult
from .ocrservice import InvalidOcrParameterError, OcrService

@asynccontextmanager
async def lifespan(app: FastAPI):
    set_handlers(app, enabled_handler)
    yield


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware, disable_for=["docs", "openapi.json"])
logger = logging.getLogger('uvicorn.error') # Use same logging as uvicorn


async def enabled_handler(enabled: bool, _: AsyncNextcloudApp) -> str:
    # Nothing to do currently ...
    logger.debug(f"App enabled: {enabled}")
    return ""

@APP.exception_handler(ExitCodeException)
async def exit_code_exception_handler(_: Request, exc: ExitCodeException):
    return JSONResponse({"message": f"{str(exc)} ({exc.__class__.__name__})", "ocrMyPdfExitCode": exc.exit_code}, status_code=500)

@APP.exception_handler(InvalidOcrParameterError)
async def invalid_ocr_parameter_exception_handler(_: Request, exc: InvalidOcrParameterError):
    # The caller sent an OCR parameter which is not allowed -> client error.
    return JSONResponse({"message": f"{str(exc)} ({exc.__class__.__name__})"}, status_code=400)

@APP.exception_handler(Exception)
async def exception_handler(_: Request, exc: Exception):
    # Exception will be logged by uvicorn automatically.
    # It will also be turned into an ErrorResult response.
    return JSONResponse({"message": f"{str(exc)} ({exc.__class__.__name__})"}, status_code=500)


@APP.post("/process_ocr", response_model=OcrResult, responses={400: {"model": ErrorResult}, 500: {"model": ErrorResult}})
async def process_ocr(
        file: UploadFile = File(..., description="The file to be processed using OCR."), 
        ocrmypdf_parameters: str = Form(None, description="Additional parameters for the OCRmyPdf process (see https://ocrmypdf.readthedocs.io/en/latest/cookbook.html#basic-examples).")
    ):
    """
    Processes an OCR request.
    This endpoint accepts a file upload and optional OCR parameters to process the file using OCR (Optical Character Recognition).
    """
    service = OcrService(logger)
    return service.ocr(file.file, file.filename, ocrmypdf_parameters)

@APP.get("/installed_languages", response_model=Iterable[str])
def installed_languages():
    """
    Retrieves the list of installed Tesseract languages - relevant for OCRmyPDF.
    """
    service = OcrService(logger)
    return service.installed_languages()