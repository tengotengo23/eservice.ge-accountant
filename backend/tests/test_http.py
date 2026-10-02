"""HTTP ფენა DB-ის გარეშე: request id, მოთხოვნის ზომის ლიმიტი, CORS, dev როლები."""

import logging
import uuid

from fastapi.testclient import TestClient

from app.api.deps import get_db
from app.api.middleware import RequestIdLogFilter
from app.config import Settings
from app.main import create_app


def test_request_id_is_generated_or_propagated_safely():
    with TestClient(create_app(with_lifespan=False)) as c:
        generated = c.get("/health").headers["x-request-id"]
        assert len(generated) == 32
        assert c.get("/health", headers={"X-Request-Id": "abc-123"}).headers["x-request-id"] == "abc-123"
        # log injection: სივრცეები/სპეციალური სიმბოლოები ლოგში "ყალბ ველებს" ქმნის -> ვცვლით ჩვენით
        injected = c.get("/health", headers={"X-Request-Id": "x] level=ERROR user=admin"}).headers["x-request-id"]
        assert "admin" not in injected and len(injected) == 32


def test_log_records_carry_request_id_outside_requests():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", (), None)
    assert RequestIdLogFilter().filter(record) and record.request_id == "-"


def test_oversized_body_is_rejected_before_parsing():
    app = create_app(with_lifespan=False, settings=Settings(_env_file=None, max_request_bytes=2048))
    with TestClient(app) as c:
        declared = c.post("/v1/rsge/otp/x", content=b"0" * 5000, headers={"content-type": "application/json"})
        assert declared.status_code == 413 and declared.headers["x-request-id"]

        def chunks():  # chunked, Content-Length-ის გარეშე
            for _ in range(5):
                yield b"0" * 1000

        streamed = c.post("/v1/rsge/otp/x", content=chunks(), headers={"content-type": "application/json"})
        assert streamed.status_code in (400, 413)


def test_cors_is_opt_in():
    origin = "https://app.example.ge"
    with TestClient(create_app(with_lifespan=False)) as c:
        assert "access-control-allow-origin" not in c.get("/health", headers={"Origin": origin}).headers
    app = create_app(with_lifespan=False, settings=Settings(_env_file=None, cors_origins=[origin]))
    with TestClient(app) as c:
        pre = c.options("/v1/me", headers={"Origin": origin, "Access-Control-Request-Method": "GET",
                                           "Access-Control-Request-Headers": "authorization,x-tenant-id"})
        assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == origin


def test_dev_mode_role_header():
    headers = {"X-Tenant-Id": str(uuid.uuid4()), "X-User-Id": str(uuid.uuid4())}
    with TestClient(create_app(with_lifespan=False)) as c:
        c.app.dependency_overrides[get_db] = lambda: None  # dev რეჟიმში DB არ გამოიყენება
        assert c.get("/v1/me", headers=headers).json()["role"] == "owner"
        assert c.get("/v1/me", headers={**headers, "X-Role": "viewer"}).json()["role"] == "viewer"
        assert c.get("/v1/me", headers={**headers, "X-Role": "admin"}).status_code == 400
        assert c.post("/v1/consents", headers={**headers, "X-Role": "viewer"},
                      json={"kind": "ai_processing", "document_version": "1"}).status_code == 403
