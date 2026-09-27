"""ლოგებიდან საიდუმლოებების ამოღება - API Key ან პაროლი ლოგში არასდროს უნდა მოხვდეს."""

import logging
import re

_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google
    re.compile(r"(?i)(password|passwd|pwd|api_key|apikey|secret|otp|code)(\"?\s*[:=]\s*\"?)[^\s\",&]+"),
]


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        if pattern.groups >= 2:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
        else:
            text = pattern.sub("***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


# uvicorn-ის logger-ებს საკუთარი handler-ები აქვთ და root-ზე არ "ადიან" (propagate=False) -
# access log-ში query string-იც ჩანს, ამიტომ ფილტრი მათაც სჭირდებათ.
_DEFAULT_LOGGERS = ("", "uvicorn", "uvicorn.error", "uvicorn.access")


def install_redaction(logger: logging.Logger | None = None) -> None:
    """idempotent: ყოველ handler-ს ერთი RedactingFilter ემატება, რამდენჯერაც არ უნდა გამოვიძახოთ."""
    targets = [logger] if logger is not None else [logging.getLogger(name) for name in _DEFAULT_LOGGERS]
    for target in targets:
        handlers = target.handlers or ([logging.lastResort] if target is logging.getLogger() else [])
        for handler in handlers:
            if handler is not None and not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter())
