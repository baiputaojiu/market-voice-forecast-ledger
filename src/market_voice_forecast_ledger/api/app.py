from __future__ import annotations

import re
import sqlite3

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from market_voice_forecast_ledger.api import dependencies
from market_voice_forecast_ledger.api.dependencies import get_connection, get_settings
from market_voice_forecast_ledger.api.routes import (
    corrections,
    health,
    heatmaps,
    jobs,
    retention,
    reviews,
    subjects,
    youtube,
)
from market_voice_forecast_ledger.config import Settings
from market_voice_forecast_ledger.domain.errors import DomainError


_SAFE_DOMAIN_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_SAFE_LOCATIONS = {
    "body",
    "query",
    "path",
    "cutoff",
    "granularity",
    "mapping_id",
    "period_id",
    "segment_id",
    "job_id",
    "decision",
    "reason",
    "corrected_asset",
    "assignment_kind",
    "assigned_subject_id",
    "preview_token",
    "subject_id",
    "url",
}
_CONFLICT_CODES = {
    "REVIEW_APPLICATION_REQUIRED",
    "DELETION_PREVIEW_NOT_CURRENT",
    "DELETION_PREVIEW_EXPIRED",
    "DELETION_PREVIEW_COMMAND_MISMATCH",
    "DELETION_PREVIEW_POLICY_CHANGED",
    "DELETION_TARGET_DRIFT",
}
_INTERNAL_CODES = {
    "ASSET_MAPPING_EVIDENCE_INVALID",
    "CURRENT_RESULT_STATE_INVALID",
    "REVIEW_APPLICATION_FAILED",
    "RETENTION_STORED_HASH_INVALID",
    "RETENTION_STORED_STATE_INVALID",
    "RETENTION_STORED_TIME_INVALID",
    "TEXT_DELETION_FAILED",
    "DELETION_PREVIEW_FAILED",
    "HEATMAP_CACHE_INVALID",
    "HEATMAP_ACTIVE_SUBJECT_SET_INVALID",
}


def create_app(settings: Settings) -> FastAPI:
    dependencies.initialize_database(settings)
    app = FastAPI(
        title="Market Voice Forecast Ledger",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(get_connection)],
    )
    app.dependency_overrides[get_connection] = dependencies.connection_dependency(
        settings
    )
    app.dependency_overrides[get_settings] = dependencies.settings_dependency(settings)
    app.state.bind_boundary = "127.0.0.1"
    app.state.authentication = "none"

    for router in (
        health.router,
        subjects.router,
        heatmaps.router,
        jobs.router,
        reviews.router,
        corrections.router,
        retention.router,
        youtube.router,
    ):
        app.include_router(router, prefix="/api")

    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(ResponseValidationError, _internal_error)
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(LookupError, _not_found_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    for exception_type in (
        sqlite3.Error,
        RuntimeError,
        TypeError,
        ValueError,
        OSError,
    ):
        app.add_exception_handler(exception_type, _internal_error)
    app.add_exception_handler(Exception, _internal_error)
    return app


async def _validation_error(
    _request: Request, error: RequestValidationError
) -> JSONResponse:
    fields: list[dict[str, str]] = []
    for item in error.errors()[:32]:
        location = ".".join(
            _safe_location(part) for part in item.get("loc", ())
        ) or "request"
        error_type = item.get("type")
        if type(error_type) is not str or not re.fullmatch(
            r"[a-z0-9_.-]{1,64}", error_type
        ):
            error_type = "invalid"
        entry = {"location": location, "type": error_type}
        if entry not in fields:
            fields.append(entry)
    return JSONResponse(
        status_code=422,
        content={"error": "REQUEST_VALIDATION_FAILED", "fields": fields},
    )


async def _domain_error(_request: Request, error: DomainError) -> JSONResponse:
    code = error.code
    if type(code) is not str or not _SAFE_DOMAIN_CODE.fullmatch(code):
        return _internal_response()
    status = _domain_status(code)
    if status == 500:
        return _internal_response()
    return JSONResponse(status_code=status, content={"error": code})


async def _not_found_error(_request: Request, _error: LookupError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"error": "NOT_FOUND"})


async def _http_error(
    _request: Request, error: StarletteHTTPException
) -> JSONResponse:
    codes = {
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
    }
    code = codes.get(error.status_code, "HTTP_ERROR")
    return JSONResponse(status_code=error.status_code, content={"error": code})


async def _internal_error(_request: Request, _error: Exception) -> JSONResponse:
    return _internal_response()


def _internal_response() -> JSONResponse:
    return JSONResponse(status_code=500, content={"error": "INTERNAL_ERROR"})


def _safe_location(value: object) -> str:
    if type(value) is int:
        return "item"
    if type(value) is str and value in _SAFE_LOCATIONS:
        return value
    return "unknown_field"


def _domain_status(code: str) -> int:
    if code == "YOUTUBE_SYNC_UNAVAILABLE":
        return 503
    if code.endswith("_NOT_FOUND"):
        return 404
    if (
        code in _INTERNAL_CODES
        or code.endswith("_STORED_INVALID")
        or code.endswith("_STORAGE_FAILED")
        or code.endswith("_DATABASE_FAILED")
    ):
        return 500
    if (
        code in _CONFLICT_CODES
        or code.endswith("_CONFLICT")
        or code.endswith("_TRANSACTION_REQUIRED")
        or code.endswith("_NOT_RUNNING")
        or code.endswith("_NOT_SUCCESSFUL")
        or code.endswith("_STALE")
    ):
        return 409
    return 422
