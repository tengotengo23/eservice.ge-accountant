"""Claude (Anthropic) ადაპტერი - ოფიციალური `anthropic` SDK, structured outputs."""

from __future__ import annotations

import base64

import anthropic

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

# `output_config.effort` მხარდაჭერილი მოდელები (Haiku 4.5 / Sonnet 4.5-ზე შეცდომას აბრუნებს).
_EFFORT_MODEL_PREFIXES = (
    "claude-fable-5", "claude-mythos-5", "claude-opus-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6", "claude-opus-4-5",
)
# სერვერული refusal-fallback (`fallbacks: "default"`) - Claude API-ზე, ამ მოდელებისთვის.
_FALLBACK_MODEL_PREFIXES = ("claude-opus-5", "claude-fable-5-1")
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicAdapter:
    provider = Provider.CLAUDE

    def __init__(self, *, timeout: float = 180.0, max_retries: int = 2, http_client=None, base_url=None):
        self._timeout = timeout
        self._max_retries = max_retries
        self._http_client = http_client
        self._base_url = base_url

    def _client(self, api_key: str) -> anthropic.AsyncAnthropic:
        # ყოველ მოთხოვნაზე ახალი კლიენტი მომხმარებლის გასაღებით (BYOK, tenant-ებს შორის გაზიარება არ ხდება)
        kwargs = {"api_key": api_key, "timeout": self._timeout, "max_retries": self._max_retries}
        if self._http_client is not None:
            kwargs["http_client"] = self._http_client
        if self._base_url is not None:
            kwargs["base_url"] = self._base_url
        return anthropic.AsyncAnthropic(**kwargs)

    @staticmethod
    def _content(prompt: str, attachments: tuple[Attachment, ...]) -> list[dict]:
        blocks: list[dict] = []
        for att in attachments:
            if att.is_pdf:
                blocks.append({
                    "type": "document",
                    "source": {"type": "base64", "media_type": att.media_type,
                               "data": base64.standard_b64encode(att.data).decode()},
                    "title": att.filename,
                })
            elif att.is_image:
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": att.media_type,
                               "data": base64.standard_b64encode(att.data).decode()},
                })
            else:
                blocks.append({"type": "text", "text": att.as_prompt_block()})
        blocks.append({"type": "text", "text": prompt})
        return blocks

    async def generate_json(self, *, api_key, model, system, prompt, attachments, schema, schema_name,
                            effort: Effort, max_output_tokens) -> RawCompletion:
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if model.startswith(_EFFORT_MODEL_PREFIXES):
            output_config["effort"] = effort.value
        params = {
            "model": model,
            "max_tokens": max_output_tokens,
            "system": system,
            "messages": [{"role": "user", "content": self._content(prompt, attachments)}],
            "output_config": output_config,
        }
        client = self._client(api_key)
        try:
            if model.startswith(_FALLBACK_MODEL_PREFIXES):
                # safety-კლასიფიკატორის უარზე API თავად გადაამისამართებს რეკომენდებულ მოდელზე
                resp = await client.beta.messages.create(**params, betas=[_FALLBACK_BETA], fallbacks="default")
            else:
                resp = await client.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError("Claude API key is invalid or revoked", provider=self.provider) from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMAuthError(f"Claude API key lacks permission: {exc.message}", provider=self.provider) from exc
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError("Claude rate limit", provider=self.provider) from exc
        except (anthropic.BadRequestError, anthropic.NotFoundError, anthropic.UnprocessableEntityError) as exc:
            raise LLMRequestError(f"Claude rejected the request: {exc.message}", provider=self.provider) from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"Claude API error {exc.status_code}", provider=self.provider) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError("Claude API unreachable", provider=self.provider) from exc

        if resp.stop_reason == "refusal":
            category = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise LLMRefusalError(f"Claude declined (category={category})", provider=self.provider)
        if resp.stop_reason == "max_tokens":
            raise LLMTruncatedError("Claude output truncated at max_tokens", provider=self.provider)

        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise LLMRequestError("Claude returned no text block", provider=self.provider)
        return RawCompletion(
            text=text,
            model=resp.model,
            usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens),
            request_id=resp._request_id,
        )

    async def list_models(self, api_key: str) -> list[str]:
        client = self._client(api_key)
        try:
            return [m.id async for m in client.models.list()]
        except anthropic.AuthenticationError as exc:
            raise LLMAuthError("Claude API key is invalid or revoked", provider=self.provider) from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"Claude API error {exc.status_code}", provider=self.provider) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError("Claude API unreachable", provider=self.provider) from exc
