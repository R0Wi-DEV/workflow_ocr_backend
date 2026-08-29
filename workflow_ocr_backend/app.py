from contextlib import asynccontextmanager
import os
import uuid
from typing import Iterable

from fastapi import FastAPI, File, Form, UploadFile, Request, Response

from fastapi.responses import JSONResponse
from nc_py_api import AsyncNextcloudApp, NextcloudApp
from nc_py_api.ex_app import AppAPIAuthMiddleware, set_handlers
import logging

from ocrmypdf import ExitCodeException
from pydantic import ValidationError

from .model.ocrresult import ErrorResult, OcrResult, ValidationErrorResult
from .ocroptions import InvalidOcrOptionsError, OcrOptions, OcrPolicy
from .ocrservice import OcrService

logger = logging.getLogger('uvicorn.error') # Use same logging as uvicorn


class ConfigurationError(RuntimeError):
    """Raised when an OCR_* environment variable can't be parsed at startup."""


def _env_number(name: str, cast: type) -> int | float | None:
    value = os.getenv(name)
    if value is None:
        return None
    try:
        return cast(value)
    except ValueError as exc:
        raise ConfigurationError(f"Environment variable {name}={value!r} is not a valid {cast.__name__}") from exc


def _policy_from_env(installed_languages: frozenset[str]) -> OcrPolicy:
    """Builds the operator-owned resource policy from the process environment.

    These are deliberately not request fields (see ocroptions.py): raising them
    re-enables the decompression-bomb guard being disabled, or lets one request
    pin the whole backend, which only takes one OCRmyPDF task at a time.
    """
    kwargs = {}
    if (v := _env_number("OCR_JOBS", int)) is not None:
        kwargs["jobs"] = v
    if (v := _env_number("OCR_MAX_IMAGE_MPIXELS", float)) is not None:
        kwargs["max_image_mpixels"] = v
    if (v := _env_number("OCR_MAX_TESSERACT_TIMEOUT_S", float)) is not None:
        kwargs["max_tesseract_timeout_s"] = v
    try:
        return OcrPolicy(installed_languages=installed_languages, **kwargs)
    except ValidationError as exc:
        # e.g. OCR_JOBS=0 or OCR_MAX_IMAGE_MPIXELS=-1 - well-formed numbers, but
        # outside OcrPolicy's own bounds. Fail fast with a clear message rather
        # than an opaque pydantic stacktrace at import time.
        raise ConfigurationError(f"Invalid OCR policy configuration: {exc}") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    set_handlers(app, enabled_handler)
    # Installed languages only change with the container image, so this is
    # read once at startup rather than shelling out to tesseract per request.
    app.state.ocr_policy = _policy_from_env(
        frozenset(OcrService(logger).installed_languages())
    )
    yield


APP = FastAPI(lifespan=lifespan)
APP.add_middleware(AppAPIAuthMiddleware, disable_for=["docs", "openapi.json"])


def get_policy() -> OcrPolicy:
    return APP.state.ocr_policy


async def enabled_handler(enabled: bool, _: AsyncNextcloudApp) -> str:
    # Nothing to do currently ...
    logger.debug(f"App enabled: {enabled}")
    return ""

@APP.exception_handler(ExitCodeException)
async def exit_code_exception_handler(_: Request, exc: ExitCodeException):
    return JSONResponse({"message": f"{str(exc)} ({exc.__class__.__name__})", "ocrMyPdfExitCode": exc.exit_code}, status_code=500)

@APP.exception_handler(InvalidOcrOptionsError)
async def invalid_ocr_options_exception_handler(_: Request, exc: InvalidOcrOptionsError):
    # The caller sent an OCR option which is not allowed or not valid right now
    # (e.g. an uninstalled language) -> client error, not a server error.
    return JSONResponse({"message": f"{str(exc)} ({exc.__class__.__name__})"}, status_code=400)

@APP.exception_handler(ValidationError)
async def validation_error_exception_handler(_: Request, exc: ValidationError):
    # Schema validation of the "options" body -> 422 with field-level detail,
    # matching FastAPI's own convention for an invalid request body.
    body = ValidationErrorResult(
        message="Invalid OCR options",
        errors=exc.errors(include_url=False, include_context=False),
    )
    return JSONResponse(body.model_dump(by_alias=True), status_code=422)

@APP.exception_handler(Exception)
async def exception_handler(_: Request, exc: Exception):
    # Never echo str(exc) here: exception text routinely carries absolute temp
    # paths and library internals. Log the full exception (message + traceback)
    # server-side against a correlation id, and return only that id to the
    # caller. exc_info is passed explicitly rather than relying on the ambient
    # sys.exc_info() - this handler runs as an awaited coroutine, and passing
    # the exception directly guarantees the right traceback is logged
    # regardless of how the framework dispatches to it.
    correlation_id = str(uuid.uuid4())
    logger.error(f"Unhandled error [{correlation_id}]", exc_info=exc)
    return JSONResponse({"message": f"Internal server error [{correlation_id}]"}, status_code=500)


@APP.post(
    "/v1/ocr",
    response_model=OcrResult,
    responses={400: {"model": ErrorResult}, 422: {"model": ValidationErrorResult}, 500: {"model": ErrorResult}},
)
def ocr_v1(
        response: Response,
        file: UploadFile = File(..., description="The file to be processed using OCR."),
        options: str = Form(
            "{}",
            description="OCR options as a JSON object validated against the OcrOptions schema "
                        "(see /docs). Unknown fields are rejected, not forwarded.",
        ),
    ):
    """
    Processes an OCR request using the typed ``OcrOptions`` schema.

    This endpoint's contract is the schema, not OCRmyPDF's own parameter set:
    every option is bounded, enumerated, or pattern-constrained, and resource
    limits (jobs, image size, timeout ceiling) are operator policy, never part
    of the request body.
    """
    parsed_options = OcrOptions.model_validate_json(options)
    # Declared synchronous on purpose: ocrmypdf.ocr() is a long-running, CPU-bound
    # blocking call. FastAPI runs a sync endpoint in the threadpool instead of on
    # the event loop, so one OCR job no longer stalls every other request
    # (including AppAPI's own heartbeat poll) for its whole duration.
    service = OcrService(logger)
    response.headers["Cache-Control"] = "no-store"
    return service.ocr(file.file, file.filename, parsed_options, get_policy())


@APP.post(
    "/process_ocr",
    response_model=OcrResult,
    responses={400: {"model": ErrorResult}, 500: {"model": ErrorResult}},
    deprecated=True,
)
def process_ocr(
        response: Response,
        file: UploadFile = File(..., description="The file to be processed using OCR."),
        ocrmypdf_parameters: str = Form(None, description="Additional parameters for the OCRmyPdf process (see https://ocrmypdf.readthedocs.io/en/latest/cookbook.html#basic-examples)."),
    ):
    """
    Deprecated. Processes an OCR request using the legacy ``--flag value`` string.

    Kept as a thin, translating shim: the string is parsed and mapped onto the
    same ``OcrOptions`` schema ``/v1/ocr`` uses, so it inherits every validation
    rule from that schema. Anything the shim cannot express (``--plugins``,
    ``--tesseract-config``, ``--unpaper-args``, ...) is a 400, not a silent
    passthrough. Use ``/v1/ocr`` for new integrations.
    """
    service = OcrService(logger)
    response.headers["Deprecation"] = "true"
    # Revisit alongside the workflow_ocr app release that switches to /v1/ocr.
    response.headers["Sunset"] = "Mon, 01 Feb 2027 00:00:00 GMT"
    response.headers["Link"] = '</v1/ocr>; rel="successor-version"'
    return service.ocr_legacy(file.file, file.filename, ocrmypdf_parameters, get_policy())

@APP.get("/installed_languages", response_model=Iterable[str])
def installed_languages():
    """
    Retrieves the list of installed Tesseract languages - relevant for OCRmyPDF.
    """
    service = OcrService(logger)
    return service.installed_languages()
