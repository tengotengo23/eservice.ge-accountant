"""ChatGPT (OpenAI) ადაპტერი - ოფიციალური `openai` SDK, Responses API + strict JSON Schema."""

from __future__ import annotations

import base64

import openai

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
)

# reasoning.effort მხოლოდ reasoning-მოდელებზე (gpt-4o-ზე შეცდომაა)
_REASONING_PREFIXES = ("gpt-6", "gpt-5", "o1", "o3", "o4")
# /v1/models აბრუნებს embedding/tts/image მოდელებსაც - UI-ში მხოლოდ ტექსტური ვაჩვენოთ
_CHAT_MODEL_PREFIXES = ("gpt-", "o1", "o3", "o4")
_NON_CHAT_MARKERS = ("embedding", "tts", "whisper", "dall-e", "image", "audio", "realtime", "transcribe", "moderation")


class OpenAIAdapter:
    provider = Provider.CHATGPT

    def __init__(self, *, timeout: float = 180.0, max_retries: int = 2, http_client=None, base_url=None):
        self._timeout = timeout
        self._max_retries = max_retries
        self._http_client = http_client
        self._base_url = base_url

    def _client(self, api_key: str) -> openai.AsyncOpenAI:
        kwargs = {"api_key": api_key, "timeout": self._timeout, "max_retries": self._max_retries}
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        return openai.AsyncOpenAI(**kwargs)

    @staticmethod
    def _content(prompt: str, attachments: tuple[Attachment, ...]) -> list[dict]:
        parts: list[dict] = []
        for att in attachments:
            b64 = base64.standard_b64encode(att.data).decode()
            if att.is_pdf:
                parts.append({"type": "input_file", "filename": att.filename,
                              "file_data": f"data:{att.media_type};base64,{b64}"})
            elif att.is_image:
                parts.append({"type": "input_image", "detail": "high",
                              "image_url": f"data:{att.media_type};base64,{b64}"})
            else:
                parts.append({"type": "input_text", "text": f'<file name="{att.filename}">\n{att.as_text()}\n</file>'})
        parts.append({"type": "input_text", "text": prompt})
        return parts

    async def generate_json(self, *, api_key, model, system, prompt, attachments, schema, schema_name,
                            effort: Effort, max_output_tokens) -> RawCompletion:
        params: dict = {
            "model": model,
            "instructions": system,
            "input": [{"role": "user", "content": self._content(prompt, attachments)}],
            "text": {"format": {"type": "json_schema", "name": schema_name, "schema": schema, "strict": True}},
            "max_output_tokens": max_output_tokens,
            "store": False,  # ფინანსური დოკუმენტები OpenAI-ის მხარეს არ ინახება
        }
        if model.startswith(_REASONING_PREFIXES):
            params["reasoning"] = {"effort": effort.value}
        client = self._client(api_key)
        try:
            resp = await client.responses.create(**params)
        except openai.AuthenticationError as exc:
            raise LLMAuthError("OpenAI API key is invalid or revoked", provider=self.provider) from exc
        except openai.PermissionDeniedError as exc:
            raise LLMAuthError("OpenAI API key lacks permission for this model", provider=self.provider) from exc
        except openai.RateLimitError as exc:
            # insufficient_quota-ც 429-ია: ბალანსი ამოიწურა - retry არ უშველის
            if getattr(exc, "code", None) == "insufficient_quota":
                raise LLMAuthError("OpenAI account has no remaining quota", provider=self.provider) from exc
            raise LLMRateLimitError("OpenAI rate limit", provider=self.provider) from exc
        except (openai.BadRequestError, openai.NotFoundError, openai.UnprocessableEntityError) as exc:
            raise LLMRequestError(f"OpenAI rejected the request: {exc.message}", provider=self.provider) from exc
        except openai.APIStatusError as exc:
            raise LLMUnavailableError(f"OpenAI API error {exc.status_code}", provider=self.provider) from exc
        except openai.APIConnectionError as exc:
            raise LLMUnavailableError("OpenAI API unreachable", provider=self.provider) from exc

        for item in resp.output or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise LLMRefusalError(f"OpenAI declined: {part.refusal}", provider=self.provider)
        if resp.status == "incomplete":
            reason = getattr(resp.incomplete_details, "reason", None)
            if reason == "content_filter":
                raise LLMRefusalError("OpenAI content filter", provider=self.provider)
            raise LLMTruncatedError(f"OpenAI output incomplete ({reason})", provider=self.provider)

        usage = Usage(resp.usage.input_tokens, resp.usage.output_tokens) if resp.usage else Usage()
        return RawCompletion(text=resp.output_text, model=resp.model, usage=usage,
                             request_id=getattr(resp, "_request_id", None))

    async def list_models(self, api_key: str) -> list[str]:
        client = self._client(api_key)
        try:
            ids = [m.id async for m in client.models.list()]
        except openai.AuthenticationError as exc:
            raise LLMAuthError("OpenAI API key is invalid or revoked", provider=self.provider) from exc
        except openai.APIStatusError as exc:
            raise LLMUnavailableError(f"OpenAI API error {exc.status_code}", provider=self.provider) from exc
        except openai.APIConnectionError as exc:
            raise LLMUnavailableError("OpenAI API unreachable", provider=self.provider) from exc
        return sorted(
            i for i in ids
            if i.startswith(_CHAT_MODEL_PREFIXES) and not any(m in i for m in _NON_CHAT_MARKERS)
        )
