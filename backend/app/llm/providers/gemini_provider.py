"""Gemini (Google) ადაპტერი - ოფიციალური `google-genai` SDK, response_json_schema."""

from __future__ import annotations

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ..base import (
    Attachment,
    Effort,
    LLMAuthError,
    LLMRateLimitError,
    LLMRefusalError,
    LLMRequestError,
    LLMTruncatedError,
    LLMUnavailableError,
    Provider,
    RawCompletion,
    Usage,
    error_for_status,
)

_REFUSAL_FINISH = {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}
# google-genai ქსელურ შეცდომებს არ ახვევს საკუთარ ტიპში: httpx.ConnectError / ReadTimeout პირდაპირ ამოდის.
# ისინი OSError-ის შვილები არ არიან, ამიტომ ცალკე ვიჭერთ (სხვაგვარად retry/fallback არ ჩაირთვება).
_NETWORK_ERRORS = (httpx.HTTPError, OSError)


class GeminiAdapter:
    provider = Provider.GEMINI

    def __init__(self, *, timeout_ms: int = 180_000, http_options: types.HttpOptions | None = None):
        self._http_options = http_options or types.HttpOptions(timeout=timeout_ms)

    def _client(self, api_key: str) -> genai.Client:
        return genai.Client(api_key=api_key, http_options=self._http_options)

    @staticmethod
    def _parts(prompt: str, attachments: tuple[Attachment, ...]) -> list[types.Part]:
        parts: list[types.Part] = []
        for att in attachments:
            if att.is_pdf or att.is_image:
                parts.append(types.Part.from_bytes(data=att.data, mime_type=att.media_type))
            else:
                parts.append(types.Part.from_text(text=att.as_prompt_block()))
        parts.append(types.Part.from_text(text=prompt))
        return parts

    def _raise_mapped(self, exc: genai_errors.APIError):
        code = getattr(exc, "code", None)
        if code in (401, 403) or (code == 400 and "API key" in str(exc)):
            raise LLMAuthError("Gemini API key is invalid or lacks permission", provider=self.provider) from exc
        if code == 429:
            raise LLMRateLimitError("Gemini rate limit / quota", provider=self.provider) from exc
        if isinstance(code, int):
            raise error_for_status(code, f"Gemini API error {code}: {exc}", self.provider) from exc
        if isinstance(exc, genai_errors.ClientError):
            raise LLMRequestError(f"Gemini rejected the request: {exc}", provider=self.provider) from exc
        raise LLMUnavailableError(f"Gemini API error {code}", provider=self.provider) from exc

    async def generate_json(self, *, api_key, model, system, prompt, attachments, schema, schema_name,
                            effort: Effort, max_output_tokens) -> RawCompletion:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
            max_output_tokens=max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # thinking_level მხოლოდ Gemini 3+ მოდელებზე; ძველებზე ნაგულისხმევი რჩება
            thinking_config=(
                types.ThinkingConfig(thinking_level=effort.value) if model.startswith("gemini-3") else None
            ),
        )
        client = self._client(api_key)
        try:
            resp = await client.aio.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=self._parts(prompt, attachments))],
                config=config,
            )
        except genai_errors.APIError as exc:
            self._raise_mapped(exc)
        except _NETWORK_ERRORS as exc:
            raise LLMUnavailableError("Gemini API unreachable", provider=self.provider) from exc

        feedback = resp.prompt_feedback
        if feedback is not None and feedback.block_reason:
            raise LLMRefusalError(f"Gemini blocked the prompt ({feedback.block_reason})", provider=self.provider)
        candidate = (resp.candidates or [None])[0]
        finish = str(getattr(candidate, "finish_reason", "") or "").split(".")[-1]
        if finish in _REFUSAL_FINISH:
            raise LLMRefusalError(f"Gemini declined (finish_reason={finish})", provider=self.provider)
        if finish == "MAX_TOKENS":
            raise LLMTruncatedError("Gemini output truncated at max_output_tokens", provider=self.provider)
        if not resp.text:
            raise LLMRequestError("Gemini returned an empty response", provider=self.provider)

        meta = resp.usage_metadata
        usage = Usage(meta.prompt_token_count or 0, meta.candidates_token_count or 0) if meta else Usage()
        return RawCompletion(text=resp.text, model=resp.model_version or model, usage=usage,
                             request_id=resp.response_id)

    async def list_models(self, api_key: str) -> list[str]:
        client = self._client(api_key)
        try:
            pager = await client.aio.models.list()
            models = [m async for m in pager]
        except genai_errors.APIError as exc:
            self._raise_mapped(exc)
        except _NETWORK_ERRORS as exc:
            raise LLMUnavailableError("Gemini API unreachable", provider=self.provider) from exc
        return sorted(
            m.name.removeprefix("models/")
            for m in models
            if "generateContent" in (m.supported_actions or []) and m.name.removeprefix("models/").startswith("gemini")
        )
