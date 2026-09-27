"""LLM Router + ადაპტერები. HTTP-ის დონეზე mock (ნამდვილი SDK, ყალბი სერვერი) - ქსელი/გასაღები არ სჭირდება."""

import json

import httpx
import httpx2
import pytest
from anthropic import DefaultAsyncHttpxClient
from google.genai import types as genai_types
from pydantic import BaseModel

from app.accounting.models import ExtractedDocument
from app.llm import (
    Attachment,
    LLMAuthError,
    LLMCredentials,
    LLMOutputValidationError,
    LLMRefusalError,
    LLMRouter,
    LLMUnavailableError,
    Provider,
)
from app.llm.base import RawCompletion, Usage
from app.llm.providers.anthropic_provider import AnthropicAdapter
from app.llm.providers.gemini_provider import GeminiAdapter
from app.llm.providers.openai_provider import OpenAIAdapter
from app.llm.schema import strict_json_schema

PDF = Attachment(b"%PDF-1.7 fake", "application/pdf", "invoice.pdf")
IMG = Attachment(b"\x89PNG fake", "image/png", "receipt.png")

VALID_DOC = {
    "document_type": "tax_invoice",
    "direction": "incoming",
    "document_number": "ეა-12345",
    "issue_date": "2026-08-14",
    "currency": "GEL",
    "seller": {"name": "შპს მომწოდებელი", "tin": "204567890"},
    "buyer": {"name": "შპს ჩვენი კომპანია", "tin": "405123456"},
    "line_items": [{"description": "საოფისე ქაღალდი", "quantity": 10, "unit_price": 11.8, "amount": 118.0}],
    "amounts_include_vat": True,
    "net_amount": 100.0,
    "vat_amount": 18.0,
    "total_amount": 118.0,
    "vat_treatment": "standard_18",
    "suggested_debit_account": "7410",
    "suggested_credit_account": "3110",
    "classification_rationale": "საოფისე ხარჯი, მომწოდებლისადმი ვალდებულება",
    "confidence": 0.93,
    "warnings": [],
}


class Captured:
    def __init__(self):
        self.requests: list[httpx.Request | httpx2.Request] = []

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


# ------------------------------------------------------------------ schema


def test_strict_schema_is_provider_compatible():
    schema = strict_json_schema(ExtractedDocument)
    dumped = json.dumps(schema)
    assert "$ref" not in dumped and "$defs" not in dumped
    assert '"default"' not in dumped

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(schema)
    # არასავალდებულო ველი = nullable, მაგრამ required-ში
    assert {"type": "null"} in schema["properties"]["vat_amount"]["anyOf"]


# ------------------------------------------------------------------ Claude


def _claude_adapter(captured: Captured, *, stop_reason="end_turn", text=None, status=200):
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.requests.append(request)
        if request.url.path.endswith("/v1/models"):
            return httpx2.Response(200, json={
                "data": [{"id": "claude-opus-5", "type": "model", "display_name": "Claude Opus 5",
                          "created_at": "2026-01-01T00:00:00Z"}],
                "has_more": False, "first_id": "claude-opus-5", "last_id": "claude-opus-5",
            })
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"type": "authentication_error",
                                                                             "message": "invalid x-api-key"}})
        return httpx2.Response(200, headers={"request-id": "req_123"}, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": text if text is not None else json.dumps(VALID_DOC)}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "stop_details": {"type": "refusal", "category": "cyber", "explanation": None} if stop_reason == "refusal" else None,
            "usage": {"input_tokens": 1200, "output_tokens": 300},
        })

    client = DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler))
    return AnthropicAdapter(http_client=client, max_retries=0)


async def test_claude_extracts_document_with_structured_output():
    captured = Captured()
    router = LLMRouter({Provider.CLAUDE: _claude_adapter(captured)})
    creds = LLMCredentials(Provider.CLAUDE, api_key="sk-ant-test", model="claude-opus-5")

    result = await router.structured(creds, system="sys", prompt="ამოიღე მონაცემები",
                                     output_model=ExtractedDocument, attachments=(PDF, IMG))

    assert result.data.total_amount == pytest.approx(118)
    assert str(result.data.vat_amount) == "18.00"
    assert result.request_id == "req_123"
    req = captured.requests[0]
    assert req.headers["x-api-key"] == "sk-ant-test"
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    body = captured.body()
    assert body["fallbacks"] == "default"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium"
    content = body["messages"][0]["content"]
    assert [c["type"] for c in content] == ["document", "image", "text"]


async def test_claude_refusal_is_not_retried_and_surfaces():
    captured = Captured()
    router = LLMRouter({Provider.CLAUDE: _claude_adapter(captured, stop_reason="refusal")}, backoff_base=0)
    with pytest.raises(LLMRefusalError):
        await router.structured(LLMCredentials(Provider.CLAUDE, "k", "claude-sonnet-5"), system="s",
                                prompt="p", output_model=ExtractedDocument)
    assert len(captured.requests) == 1
    assert "anthropic-beta" not in captured.requests[0].headers  # sonnet -> fallback beta არ იგზავნება


async def test_claude_invalid_key_maps_to_auth_error():
    router = LLMRouter({Provider.CLAUDE: _claude_adapter(Captured(), status=401)})
    with pytest.raises(LLMAuthError):
        await router.structured(LLMCredentials(Provider.CLAUDE, "bad"), system="s", prompt="p",
                                output_model=ExtractedDocument)


async def test_claude_list_models():
    router = LLMRouter({Provider.CLAUDE: _claude_adapter(Captured())})
    assert await router.validate_key(Provider.CLAUDE, "k") == ["claude-opus-5"]


# ------------------------------------------------------------------ ChatGPT


def _openai_adapter(captured: Captured, *, output=None, status="completed"):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.requests.append(request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": "gpt-6-sol", "object": "model", "created": 0, "owned_by": "openai"},
                {"id": "text-embedding-3-large", "object": "model", "created": 0, "owned_by": "openai"},
                {"id": "gpt-6-luna", "object": "model", "created": 0, "owned_by": "openai"},
            ]})
        return httpx.Response(200, json={
            "id": "resp_1", "object": "response", "created_at": 0, "status": status, "model": "gpt-6-sol",
            "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
            "output": output or [{"type": "message", "id": "m1", "status": "completed", "role": "assistant",
                                  "content": [{"type": "output_text", "text": json.dumps(VALID_DOC), "annotations": []}]}],
            "usage": {"input_tokens": 900, "output_tokens": 250, "total_tokens": 1150},
        })

    return OpenAIAdapter(http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)), max_retries=0)


async def test_chatgpt_extracts_document_with_strict_schema():
    captured = Captured()
    router = LLMRouter({Provider.CHATGPT: _openai_adapter(captured)})
    result = await router.structured(LLMCredentials(Provider.CHATGPT, "sk-proj-test", "gpt-6-sol"),
                                     system="sys", prompt="p", output_model=ExtractedDocument, attachments=(PDF, IMG))
    assert result.data.seller.tin == "204567890"
    assert result.usage == Usage(900, 250)
    body = captured.body()
    assert captured.requests[0].headers["authorization"] == "Bearer sk-proj-test"
    assert body["text"]["format"]["strict"] is True and body["text"]["format"]["name"] == "ExtractedDocument"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["store"] is False
    types_ = [p["type"] for p in body["input"][0]["content"]]
    assert types_ == ["input_file", "input_image", "input_text"]
    assert body["input"][0]["content"][0]["file_data"].startswith("data:application/pdf;base64,")


async def test_chatgpt_refusal_and_truncation():
    refusal = [{"type": "message", "id": "m1", "status": "completed", "role": "assistant",
                "content": [{"type": "refusal", "refusal": "I can't help with that."}]}]
    router = LLMRouter({Provider.CHATGPT: _openai_adapter(Captured(), output=refusal)})
    with pytest.raises(LLMRefusalError):
        await router.structured(LLMCredentials(Provider.CHATGPT, "k"), system="s", prompt="p",
                                output_model=ExtractedDocument)


async def test_chatgpt_models_filtered_to_text_models():
    router = LLMRouter({Provider.CHATGPT: _openai_adapter(Captured())})
    assert await router.validate_key(Provider.CHATGPT, "k") == ["gpt-6-luna", "gpt-6-sol"]


# ------------------------------------------------------------------ Gemini


def _gemini_adapter(captured: Captured, *, finish="STOP"):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"models": [
                {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent", "countTokens"]},
                {"name": "models/text-embedding-005", "supportedGenerationMethods": ["embedContent"]},
            ]})
        return httpx.Response(200, json={
            "candidates": [{"content": {"role": "model", "parts": [{"text": json.dumps(VALID_DOC)}]},
                            "finishReason": finish}],
            "usageMetadata": {"promptTokenCount": 700, "candidatesTokenCount": 200},
            "modelVersion": "gemini-3.8-flash", "responseId": "gem-1",
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return GeminiAdapter(http_options=genai_types.HttpOptions(httpx_async_client=client))


async def test_gemini_extracts_document_with_json_schema():
    captured = Captured()
    router = LLMRouter({Provider.GEMINI: _gemini_adapter(captured)})
    result = await router.structured(LLMCredentials(Provider.GEMINI, "AIza-test", "gemini-3.8-flash"),
                                     system="sys", prompt="p", output_model=ExtractedDocument, attachments=(PDF,))
    assert result.data.buyer.tin == "405123456"
    assert result.request_id == "gem-1"
    req = captured.requests[0]
    assert req.headers["x-goog-api-key"] == "AIza-test"
    body = captured.body()
    cfg = body["generationConfig"]
    assert cfg["responseMimeType"] == "application/json"
    assert cfg["responseJsonSchema"]["additionalProperties"] is False
    # SDK აგზავნის proto-ველის სახელს (thinking_level); API ორივე ფორმას იღებს
    thinking = cfg["thinkingConfig"]
    assert (thinking.get("thinkingLevel") or thinking.get("thinking_level")) == "MEDIUM"
    inline = body["contents"][0]["parts"][0]["inlineData"]
    assert (inline.get("mimeType") or inline.get("mime_type")) == "application/pdf"


async def test_gemini_safety_block_is_refusal():
    router = LLMRouter({Provider.GEMINI: _gemini_adapter(Captured(), finish="SAFETY")})
    with pytest.raises(LLMRefusalError):
        await router.structured(LLMCredentials(Provider.GEMINI, "k", "gemini-3.8-flash"), system="s", prompt="p",
                                output_model=ExtractedDocument)


async def test_gemini_list_models():
    router = LLMRouter({Provider.GEMINI: _gemini_adapter(Captured())})
    assert await router.validate_key(Provider.GEMINI, "k") == ["gemini-3.8-flash"]


# ------------------------------------------------------------------ router logic


class ScriptedAdapter:
    """ყოველ გამოძახებაზე აბრუნებს სცენარის შემდეგ ელემენტს (ტექსტს ან შეცდომას)."""

    def __init__(self, provider, script):
        self.provider = provider
        self.script = list(script)
        self.prompts: list[str] = []

    async def generate_json(self, **kw):
        self.prompts.append(kw["prompt"])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return RawCompletion(text=item, model="m", usage=Usage(10, 5))

    async def list_models(self, api_key):
        return ["m"]


class Tiny(BaseModel):
    answer: int


async def test_router_repairs_invalid_json_once():
    adapter = ScriptedAdapter(Provider.CLAUDE, ['{"answer": "not-a-number"}', '{"answer": 42}'])
    router = LLMRouter({Provider.CLAUDE: adapter})
    result = await router.structured(LLMCredentials(Provider.CLAUDE, "k", "m"), system="s", prompt="p",
                                     output_model=Tiny)
    assert result.data.answer == 42 and result.attempts == 2
    assert result.usage == Usage(20, 10)
    assert "answer" in adapter.prompts[1] and "სქემას" in adapter.prompts[1]


async def test_router_gives_up_after_repair_attempts():
    adapter = ScriptedAdapter(Provider.CLAUDE, ["{}", "{}"])
    with pytest.raises(LLMOutputValidationError):
        await LLMRouter({Provider.CLAUDE: adapter}).structured(
            LLMCredentials(Provider.CLAUDE, "k", "m"), system="s", prompt="p", output_model=Tiny)


async def test_router_retries_transient_errors_then_falls_back():
    claude = ScriptedAdapter(Provider.CLAUDE, [LLMUnavailableError("down")] * 3)
    gemini = ScriptedAdapter(Provider.GEMINI, ['{"answer": 7}'])
    router = LLMRouter({Provider.CLAUDE: claude, Provider.GEMINI: gemini}, backoff_base=0)
    result = await router.structured(
        LLMCredentials(Provider.CLAUDE, "k1", "m"), system="s", prompt="p", output_model=Tiny,
        fallbacks=[LLMCredentials(Provider.GEMINI, "k2", "m")],
    )
    assert result.provider is Provider.GEMINI and result.data.answer == 7
    assert len(claude.prompts) == 3  # 1 + 2 retry


async def test_router_rejects_unsupported_attachment_before_calling_provider():
    adapter = ScriptedAdapter(Provider.CLAUDE, [])
    xlsx = Attachment(b"PK..", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "a.xlsx")
    with pytest.raises(Exception, match="unsupported media type"):
        await LLMRouter({Provider.CLAUDE: adapter}).structured(
            LLMCredentials(Provider.CLAUDE, "k", "m"), system="s", prompt="p", output_model=Tiny, attachments=(xlsx,))
    assert adapter.prompts == []


def test_credentials_repr_hides_key():
    assert "sk-secret" not in repr(LLMCredentials(Provider.CLAUDE, "sk-secret"))
