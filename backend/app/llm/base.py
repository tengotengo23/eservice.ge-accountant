"""LLM Router-ის საერთო ტიპები. პროვაიდერ-სპეციფიკური კოდი მხოლოდ providers/-შია."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel


class Provider(str, Enum):
    CLAUDE = "claude"
    CHATGPT = "chatgpt"
    GEMINI = "gemini"


class Effort(str, Enum):
    """რამდენი "ფიქრი" დაიხარჯოს. თითოეული ადაპტერი თავის პარამეტრზე ასახავს."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Attachment:
    data: bytes
    media_type: str  # application/pdf | image/jpeg | image/png | image/webp | text/csv | text/plain
    filename: str

    @property
    def is_pdf(self) -> bool:
        return self.media_type == "application/pdf"

    @property
    def is_image(self) -> bool:
        return self.media_type.startswith("image/")

    @property
    def is_text(self) -> bool:
        return self.media_type.startswith("text/")

    def as_text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class LLMCredentials:
    """მომხმარებლის საკუთარი გასაღები (BYOK). გაშიფრულია მხოლოდ worker-ის მეხსიერებაში."""

    provider: Provider
    api_key: str = field(repr=False)
    model: str | None = None
    credential_id: str | None = None  # llm_credentials.id - ხარჯის აღრიცხვისთვის (llm_calls)


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class RawCompletion:
    text: str
    model: str
    usage: Usage
    request_id: str | None = None


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    data: T
    provider: Provider
    model: str
    usage: Usage
    attempts: int
    latency_ms: int
    request_id: str | None = None


class ProviderAdapter(Protocol):
    provider: Provider

    async def generate_json(
        self,
        *,
        api_key: str,
        model: str,
        system: str,
        prompt: str,
        attachments: tuple[Attachment, ...],
        schema: dict,
        schema_name: str,
        effort: Effort,
        max_output_tokens: int,
    ) -> RawCompletion: ...

    async def list_models(self, api_key: str) -> list[str]: ...


# ---------------------------------------------------------------- errors


class LLMError(Exception):
    retryable: bool = False

    def __init__(self, message: str, *, provider: Provider | None = None):
        super().__init__(message)
        self.provider = provider


class LLMAuthError(LLMError):
    """არასწორი/გაუქმებული API Key - მომხმარებელს ვაცნობებთ, ხელახლა არ ვცდით."""


class LLMRateLimitError(LLMError):
    retryable = True


class LLMUnavailableError(LLMError):
    retryable = True


class LLMRequestError(LLMError):
    """400/404: არასწორი მოდელი, ზედმეტად დიდი ფაილი და ა.შ."""


class LLMRefusalError(LLMError):
    """მოდელმა უარი თქვა (safety). დოკუმენტი გადადის ადამიანის განხილვაზე."""


class LLMTruncatedError(LLMError):
    """პასუხი max_output_tokens-ზე გაწყდა."""


class LLMOutputValidationError(LLMError):
    """მოდელის JSON არ შეესაბამება სქემას (რამდენიმე მცდელობის შემდეგაც)."""
