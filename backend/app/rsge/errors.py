"""rs.ge ავტომატიზაციის შეცდომები. თითოეული ასახავს კონკრეტულ, მომხმარებლისთვის ახსნად მდგომარეობას."""

from __future__ import annotations

from pathlib import Path


class RsGeError(Exception):
    code = "rsge_error"

    def __init__(self, message: str, *, screenshot: Path | None = None):
        super().__init__(message)
        self.screenshot = screenshot


class HumanInterventionRequired(RsGeError):
    """CAPTCHA, უცნობი გვერდი, პაროლის შეცვლის მოთხოვნა... - ავტომატიკა ჩერდება, მომხმარებელს ვატყობინებთ."""

    code = "human_intervention_required"

    def __init__(self, reason: str, message: str, *, screenshot: Path | None = None):
        super().__init__(message, screenshot=screenshot)
        self.reason = reason


class InvalidCredentialsError(RsGeError):
    """არასწორი მომხმარებელი/პაროლი. ხელახლა არ ვცდით (ანგარიშის დაბლოკვის რისკი)."""

    code = "invalid_credentials"


class OtpTimeoutError(RsGeError):
    code = "otp_timeout"


class OtpRejectedError(RsGeError):
    """SMS კოდი არასწორია. ავტომატურად არ ვიმეორებთ - brute force აკრძალულია."""

    code = "otp_rejected"


class PortalLayoutChangedError(RsGeError):
    """მოსალოდნელი ელემენტი ვერ მოიძებნა - rs.ge-მ, სავარაუდოდ, ინტერფეისი შეცვალა."""

    code = "portal_layout_changed"


class AlreadySubmittedError(RsGeError):
    """ამ ტიპის და პერიოდის დეკლარაცია უკვე წარდგენილია (idempotency)."""

    code = "already_submitted"


class PrefilledDataError(RsGeError):
    """rs.ge-მ ფორმა ავტომატურად შეავსო არანულოვანი მონაცემებით - ნულოვანი დეკლარაცია არასწორი იქნება."""

    code = "prefilled_nonzero_data"

    def __init__(self, fields: dict[str, str], *, screenshot: Path | None = None):
        super().__init__(f"non-zero prefilled fields: {sorted(fields)}", screenshot=screenshot)
        self.fields = fields


class FingerprintMismatchError(RsGeError):
    """ფორმის შიგთავსი შეიცვალა მომხმარებლის დადასტურების შემდეგ - წარდგენა შეჩერებულია."""

    code = "fingerprint_mismatch"


class ApprovalError(RsGeError):
    code = "approval_invalid"
