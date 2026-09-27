"""ASGI middleware: request ID (ლოგების კორელაცია) და მოთხოვნის ზომის ლიმიტი."""

from __future__ import annotations

import json
import logging
import re
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
# გარედან მოსული ID ლოგში იწერება - მხოლოდ უსაფრთხო სიმბოლოები (log injection-ის წინააღმდეგ)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")


class RequestIdMiddleware:
    """X-Request-Id: შემოსულს ვიყენებთ (თუ ვალიდურია) ან ვქმნით ახალს; ბრუნდება პასუხში და ჩანს ლოგებში."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)

        async def send_with_id(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), (b"x-request-id", rid.encode())]}
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_id_var.reset(token)


class RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """ატვირთვის ლიმიტი მანამდე, სანამ Starlette ფაილს დისკზე ჩაწერს.

    Content-Length-ით -> 413 მაშინვე. chunked მოთხოვნაზე ბაიტებს ვითვლით; ლიმიტის გადაჭარბებისას
    body-ს კითხვა წყდება (FastAPI ამას შეიძლება 400-ად აჩვენოს - მთავარია, რომ რესურსი არ იხარჯება).
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.max_bytes:
            return await self._reject(send)

        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if started:
                raise
            await self._reject(send)

    async def _reject(self, send) -> None:
        body = json.dumps({"detail": f"request body exceeds {self.max_bytes} bytes"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
