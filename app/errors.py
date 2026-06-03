"""Centralised fault handlers — 5xx become graceful 503 with request_id, never raw traces."""
from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", None) or str(uuid.uuid4())


def attach_fault_handlers(application: FastAPI) -> None:
    logger = structlog.get_logger()

    @application.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "error": "validation_error",
                "detail": exc.errors(),
                "request_id": _trace_id(request),
            },
        )

    @application.exception_handler(SQLAlchemyError)
    async def on_database_fault(request: Request, exc: SQLAlchemyError):
        rid = _trace_id(request)
        logger.error("database_error", request_id=rid, error_class=type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={
                "error": "service_unavailable",
                "message": "Database temporarily unavailable. Retry shortly.",
                "request_id": rid,
            },
        )

    @application.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": "http_error",
                "message": exc.detail,
                "request_id": _trace_id(request),
            },
        )

    @application.exception_handler(Exception)
    async def on_unhandled_fault(request: Request, exc: Exception):
        rid = _trace_id(request)
        logger.error("unhandled_exception", request_id=rid, error_class=type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_error",
                "message": "An internal error occurred.",
                "request_id": rid,
            },
        )
