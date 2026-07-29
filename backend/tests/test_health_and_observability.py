import json
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.health import router as health_router
from app.observability import (
    JsonFormatter,
    configure_logging,
    request_logging_middleware,
)


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.middleware("http")(request_logging_middleware)
        app.include_router(health_router)
        self.client = TestClient(app)

    def test_health_reports_process_alive_and_returns_request_id(self):
        response = self.client.get(
            "/health",
            headers={"X-Request-ID": "probe-123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(response.headers["X-Request-ID"], "probe-123")

    def test_ready_checks_sqlite(self):
        response = self.client.get("/ready")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"status": "ready", "checks": {"sqlite": "ok"}},
        )

    def test_ready_returns_503_when_sqlite_is_unavailable(self):
        with patch("app.api.health.engine.connect", side_effect=OSError):
            response = self.client.get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "database unavailable")


class StructuredLoggingTests(unittest.TestCase):
    def test_configure_logging_disables_query_bearing_uvicorn_access_log(self):
        import logging

        configure_logging()
        self.assertTrue(logging.getLogger("uvicorn.access").disabled)
        self.assertEqual(logging.getLogger("httpx").level, logging.WARNING)

    def test_formatter_emits_json(self):
        import logging

        record = logging.LogRecord(
            "librovia.test",
            logging.INFO,
            __file__,
            1,
            "completed",
            (),
            None,
        )
        record.job_id = "job-1"
        record.platform = "kobo"
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload["message"], "completed")
        self.assertEqual(payload["job_id"], "job-1")
        self.assertEqual(payload["platform"], "kobo")

    def test_formatter_does_not_emit_exception_message(self):
        import logging

        try:
            raise RuntimeError("secret-token-value")
        except RuntimeError:
            import sys

            record = logging.LogRecord(
                "librovia.test",
                logging.ERROR,
                __file__,
                1,
                "request failed",
                (),
                sys.exc_info(),
            )
        payload = JsonFormatter().format(record)
        self.assertNotIn("secret-token-value", payload)
        self.assertEqual(json.loads(payload)["exception_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
