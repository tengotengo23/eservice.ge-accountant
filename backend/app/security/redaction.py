"""ლოგებიდან საიდუმლოებების ამოღება - API Key ან პაროლი ლოგში არასდროს უნდა მოხვდეს."""

import logging
import re

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=\-]{8,}"),  # Authorization: Bearer <JWT/token>
    re.compile(r"(?i)(x-api-key|x-goog-api-key|authorization)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",&]+"),
    re.compile(r"(?i)(password|passwd|pwd|api_key|apikey|secret|otp|code)(\"?\s*[:=]\s*\"?)[^\s\",&]+"),
]


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
        elif pattern.groups == 1:
            text = pattern.sub(lambda m: f"{m.group(1)}***", text)
        else:
            text = pattern.sub("***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        # traceback-ი ცალკე ფორმატირდება და შეიძლება შეიცავდეს გასაღებს (მაგ. SDK-ის შეცდომის ტექსტში).
        # Formatter იყენებს წინასწარ შევსებულ exc_text-ს, ამიტომ აქვე ვაფორმატებთ და ვწმენდთ.
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def install_redaction(logger: logging.Logger | None = None) -> None:
    target = logger or logging.getLogger()
    for handler in target.handlers or [logging.lastResort]:
        if handler is not None and not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(RedactingFilter())
