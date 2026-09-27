"""LLM Router: ერთი ინტერფეისი Claude / ChatGPT / Gemini-სთვის.

    router = LLMRouter()
    creds = LLMCredentials(Provider.CLAUDE, api_key="sk-ant-...", model="claude-opus-5")
    result = await router.structured(creds, system=..., prompt=..., output_model=ExtractedDocument,
                                     attachments=(Attachment(pdf_bytes, "application/pdf", "inv.pdf"),))
    result.data  # -> ვალიდირებული ExtractedDocument

პრინციპები:
* მოდელის პასუხი ყოველთვის გადის Pydantic ვალიდაციას; წარუმატებლობისას - ერთი "repair" მცდელობა
  შეცდომის ტექსტით. ვალიდაციას ვერ გაცდენილი JSON სისტემაში არ შედის.
* retry მხოლოდ დროებით შეცდომებზე (429/5xx/ქსელი); არასწორ გასაღებზე - მაშინვე ვჩერდებით.
* fallback სხვა პროვაიდერზე მხოლოდ მაშინ, თუ tenant-ს ჩართული აქვს და მისი გასაღებიც გვაქვს
  (სხვა კომპანიის მონაცემები სხვა პროვაიდერთან მისი თანხმობის გარეშე არ იგზავნება).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Sequence

from pydantic import ValidationError

from ..config import get_settings
from .base import (
    Attachment,
    Effort,
    LLMCredentials,
    LLMError,
    LLMOutputValidationError,
    LLMRequestError,
    Provider,
    ProviderAdapter,
    RawCompletion,
    StructuredResult,
    T,
    Usage,
)
from .schema import strict_json_schema

log = logging.getLogger(__name__)

_MAX_ATTACHMENT_BYTES = 30 * 1024 * 1024  # ყველაზე მკაცრი ლიმიტი სამ პროვაიდერს შორის (Claude ~32MB request)
_SUPPORTED_BINARY = {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif"}


def default_adapters() -> dict[Provider, ProviderAdapter]:
    from .providers.anthropic_provider import AnthropicAdapter
    from .providers.gemini_provider import GeminiAdapter
    from .providers.openai_provider import OpenAIAdapter

    return {
        Provider.CLAUDE: AnthropicAdapter(),
        Provider.CHATGPT: OpenAIAdapter(),
        Provider.GEMINI: GeminiAdapter(),
    }


def default_model_for(provider: Provider) -> str:
    s = get_settings()
    return {
        Provider.CLAUDE: s.default_model_claude,
        Provider.CHATGPT: s.default_model_chatgpt,
        Provider.GEMINI: s.default_model_gemini,
    }[provider]


class LLMRouter:
    def __init__(
        self,
        adapters: dict[Provider, ProviderAdapter] | None = None,
        *,
        transient_retries: int = 2,
        backoff_base: float = 1.5,
    ):
        self._adapters = adapters if adapters is not None else default_adapters()
        self._transient_retries = transient_retries
        self._backoff_base = backoff_base

    def adapter(self, provider: Provider) -> ProviderAdapter:
        try:
            return self._adapters[provider]
        except KeyError:
            raise LLMRequestError(f"provider {provider.value!r} is not configured") from None

    async def validate_key(self, provider: Provider, api_key: str) -> list[str]:
        """გასაღების შემოწმება (იაფი /models გამოძახება) - ბრუნდება ხელმისაწვდომი მოდელების სია."""
        return await self.adapter(provider).list_models(api_key)

    async def structured(
        self,
        creds: LLMCredentials,
        *,
        system: str,
        prompt: str,
        output_model: type[T],
        attachments: Sequence[Attachment] = (),
        effort: Effort = Effort.MEDIUM,
        max_output_tokens: int = 16_000,
        repair_attempts: int = 1,
        fallbacks: Sequence[LLMCredentials] = (),
    ) -> StructuredResult[T]:
        """მოდელიდან ვალიდირებული `output_model` ობიექტის მიღება.

        fallbacks - იგივე tenant-ის სხვა პროვაიდერის გასაღებები (მხოლოდ მისი თანხმობით).
        """
        attachments = tuple(attachments)
        self._check_attachments(attachments)
        chain = [creds, *fallbacks]
        for index, current in enumerate(chain):
            try:
                return await self._structured_single(
                    current, system=system, prompt=prompt, output_model=output_model,
                    attachments=attachments, effort=effort, max_output_tokens=max_output_tokens,
                    repair_attempts=repair_attempts,
                )
            except LLMError as exc:
                has_next = index + 1 < len(chain)
                # fallback აზრიანია მიუწვდომლობაზე/უარზე; არასწორი მოთხოვნა სხვაგანაც ჩავარდება
                if not has_next or isinstance(exc, LLMRequestError):
                    raise
                log.warning("LLM provider %s failed (%s); falling back to %s",
                            current.provider.value, type(exc).__name__, chain[index + 1].provider.value)
        raise AssertionError("unreachable: the last provider in the chain always re-raises")

    async def _structured_single(self, creds, *, system, prompt, output_model: type[T], attachments,
                                 effort, max_output_tokens, repair_attempts) -> StructuredResult[T]:
        adapter = self.adapter(creds.provider)
        model = creds.model or default_model_for(creds.provider)
        schema = strict_json_schema(output_model)
        schema_name = output_model.__name__
        started = time.monotonic()
        total = Usage()
        current_prompt = prompt

        for attempt in range(1, repair_attempts + 2):
            raw = await self._call_with_retry(
                adapter, api_key=creds.api_key, model=model, system=system, prompt=current_prompt,
                attachments=attachments, schema=schema, schema_name=schema_name, effort=effort,
                max_output_tokens=max_output_tokens,
            )
            total = Usage(total.input_tokens + raw.usage.input_tokens, total.output_tokens + raw.usage.output_tokens)
            try:
                data = output_model.model_validate_json(raw.text)
            except ValidationError as exc:
                log.info("LLM output failed validation (attempt %d): %s", attempt, exc.error_count())
                current_prompt = _repair_prompt(prompt, raw.text, exc)
                continue
            return StructuredResult(
                data=data, provider=creds.provider, model=raw.model, usage=total, attempts=attempt,
                latency_ms=int((time.monotonic() - started) * 1000), request_id=raw.request_id,
            )
        raise LLMOutputValidationError(
            f"{creds.provider.value} output did not match {schema_name} after {repair_attempts + 1} attempts",
            provider=creds.provider,
        )

    async def _call_with_retry(self, adapter: ProviderAdapter, **kwargs) -> RawCompletion:
        # SDK-ებს თავიანთი retry აქვთ; ეს ფენა ფარავს მათ ამოწურვას (მაგ. ხანგრძლივი 529/overload)
        for attempt in range(self._transient_retries + 1):
            try:
                return await adapter.generate_json(**kwargs)
            except LLMError as exc:
                if not exc.retryable or attempt == self._transient_retries:
                    raise
                delay = self._backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning("transient LLM error (%s); retry in %.1fs", type(exc).__name__, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    @staticmethod
    def _check_attachments(attachments: tuple[Attachment, ...]) -> None:
        for att in attachments:
            if len(att.data) > _MAX_ATTACHMENT_BYTES:
                raise LLMRequestError(f"{att.filename}: file too large for LLM processing; split it first")
            if not (att.is_text or att.media_type in _SUPPORTED_BINARY):
                raise LLMRequestError(
                    f"{att.filename}: unsupported media type {att.media_type} (convert Excel/Word to CSV/PDF first)"
                )


def _repair_prompt(original_prompt: str, bad_output: str, error: ValidationError) -> str:
    problems = [
        {"field": ".".join(str(p) for p in e["loc"]), "error": e["msg"]} for e in error.errors()[:20]
    ]
    return (
        f"{original_prompt}\n\n"
        "შენი წინა პასუხი არ შეესაბამებოდა სქემას. გაასწორე მხოლოდ ეს შეცდომები და დააბრუნე სრული JSON:\n"
        f"{json.dumps(problems, ensure_ascii=False)}\n"
        f"წინა პასუხი:\n{bad_output[:4000]}"
    )
