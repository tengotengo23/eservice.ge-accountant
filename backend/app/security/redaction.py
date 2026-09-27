"""ლოგებიდან საიდუმლოებების ამოღება - API Key ან პაროლი ლოგში არასდროს უნდა მოხვდეს."""

import logging
import re
from collections.abc import Mapping
from decimal import Decimal

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


def _redact_arg(value):
    # რიცხვები ხელუხლებელია (%d ფორმატი); დანარჩენი -> რედაქტირებული სტრიქონი
    if value is None or isinstance(value, (bool, int, float, Decimal)):
        return value
    return redact(value if isinstance(value, str) else str(value))


class RedactingFilter(logging.Filter):
    """ნაგულისხმევად: ჯერ ფორმატირება, მერე რედაქტირება (ასე "password=%s" + arg-იც იფარება).

    preserve_args=True - formatter-ებისთვის, რომლებიც record.args-ს პირდაპირ "ხსნიან" (uvicorn-ის
    AccessFormatter: 5 ველი). ასეთ დროს თითო არგუმენტი რედაქტირდება და args-ის ფორმა ნარჩუნდება.
    """

    def __init__(self, *, preserve_args: bool = False):
        super().__init__()
        self.preserve_args = preserve_args

    def filter(self, record: logging.LogRecord) -> bool:
        if self.preserve_args:
            record.msg = redact(record.msg if isinstance(record.msg, str) else str(record.msg))
            if isinstance(record.args, Mapping):
                record.args = {k: _redact_arg(v) for k, v in record.args.items()}
            elif record.args:
                record.args = tuple(_redact_arg(a) for a in record.args)
            return True
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - გაუმართავი log გამოძახება მოთხოვნას არ უნდა "აგდებდეს"
            return True  # handler-ი თავად აჩვენებს ფორმატირების შეცდომას (handleError)
        record.msg = redact(message)
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
        preserve_args = target.name == "uvicorn.access"
        for handler in handlers:
            if handler is not None and not any(isinstance(f, RedactingFilter) for f in handler.filters):
                handler.addFilter(RedactingFilter(preserve_args=preserve_args))
