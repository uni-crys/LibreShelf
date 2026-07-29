from __future__ import annotations

import inspect
import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import Request

request_id_context: ContextVar[str | None] = ContextVar(
    "request_id",
    default=None,
)
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
STANDARD_LOG_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line without serializing arbitrary objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None) or request_id_context.get()
        if request_id:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in STANDARD_LOG_RECORD_FIELDS and key != "request_id":
                if isinstance(value, (str, int, float, bool)) or value is None:
                    payload[key] = value
        if record.exc_info:
            # Exception messages from browsers/upstream APIs may echo a URL,
            # header, or credential. Keep the searchable type, not its text.
            payload["exception_type"] = record.exc_info[0].__name__
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    # Uvicorn includes the full query string in its access log. Our middleware
    # already records a query-free request event, so disable the duplicate to
    # avoid leaking user identifiers or other query parameters.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx's INFO log includes complete outbound URLs and their query strings,
    # which can contain book titles, ISBNs, or API keys.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _request_id(value: str | None) -> str:
    if value and SAFE_REQUEST_ID.fullmatch(value):
        return value
    return str(uuid.uuid4())


async def request_logging_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
):
    request_id = _request_id(request.headers.get("X-Request-ID"))
    token = request_id_context.set(request_id)
    started = time.perf_counter()
    logger = logging.getLogger("librovia.http")
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "HTTP request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        return response
    except Exception:
        logger.exception(
            "HTTP request failed",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": 500,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        raise
    finally:
        request_id_context.reset(token)


async def run_sync_job(
    operation: str,
    platform: str,
    worker: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run a sync worker and emit searchable, credential-free lifecycle logs."""

    job_id = str(uuid.uuid4())
    started = time.perf_counter()
    logger = logging.getLogger("librovia.sync")
    base = {
        "job_id": job_id,
        "operation": operation,
        "platform": platform,
    }
    logger.info("Sync job started", extra={**base, "result": "started"})
    try:
        result = worker(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
        outcome = result.get("status", "success") if isinstance(result, dict) else "success"
        logger.info(
            "Sync job completed",
            extra={
                **base,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "result": outcome,
            },
        )
        return result
    except Exception:
        logger.exception(
            "Sync job failed",
            extra={
                **base,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "result": "failed",
            },
        )
        raise
