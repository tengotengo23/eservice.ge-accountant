"""დეტერმინისტული შემოწმებები LLM-ის მიერ ამოღებულ მონაცემებზე.

წესი: LLM "კითხულობს" და "გვთავაზობს", მაგრამ არითმეტიკას, ფორმატებს და ლოგიკურ
თანმიმდევრულობას ამოწმებს კოდი. ნებისმიერი error -> დოკუმენტი ადამიანის განხილვაზე.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from .models import Direction, DocumentType, ExtractedDocument, VatTreatment

VAT_RATE = Decimal("0.18")
_TIN_RE = re.compile(r"^(\d{9}|\d{11})$")  # 9 - იურიდიული პირი, 11 - ფიზიკური პირის პირადი ნომერი
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_CENT = Decimal("0.01")

# ⚠️ საილუსტრაციო ქვესიმრავლე. production-ში კლასიფიკაცია ხდება tenant-ის საკუთარი
# ანგარიშთა გეგმით (DB: chart_of_accounts), რომელსაც ბუღალტერი ამტკიცებს.
SAMPLE_CHART_OF_ACCOUNTS: dict[str, str] = {
    "1110": "ეროვნული ვალუტა სალაროში",
    "1210": "ეროვნული ვალუტა ბანკში",
    "1220": "უცხოური ვალუტა ბანკში",
    "1410": "მოთხოვნები მიწოდებიდან და მომსახურებიდან",
    "1610": "საქონელი",
    "3110": "მიწოდებიდან და მომსახურებიდან წარმოქმნილი ვალდებულებები",
    "3130": "გადასახდელი ხელფასები",
    "3310": "გადასახდელი მოგების გადასახადი",
    "3320": "გადასახდელი საშემოსავლო გადასახადი",
    "3330": "გადასახდელი დღგ",
    "3340": "ჩასათვლელი დღგ",
    "5110": "საწესდებო კაპიტალი",
    "6110": "შემოსავალი რეალიზაციიდან",
    "7110": "რეალიზებული საქონლის თვითღირებულება",
    "7410": "საერთო და ადმინისტრაციული ხარჯები",
}


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_tin(tin: str | None) -> bool:
    return tin is None or bool(_TIN_RE.match(tin))


def _close(a: Decimal, b: Decimal, tolerance: Decimal) -> bool:
    return abs(a - b) <= tolerance


def validate_extracted_document(
    doc: ExtractedDocument,
    *,
    our_tin: str,
    today: date,
    chart_of_accounts: dict[str, str] = SAMPLE_CHART_OF_ACCOUNTS,
) -> ValidationReport:
    r = ValidationReport()

    # --- საიდენტიფიკაციო კოდები
    for role, party in (("გამყიდველის", doc.seller), ("მყიდველის", doc.buyer)):
        if not validate_tin(party.tin):
            r.errors.append(f"{role} საიდენტიფიკაციო კოდი არასწორი ფორმატისაა: {party.tin!r}")
    if doc.direction is Direction.INCOMING and doc.buyer.tin and doc.buyer.tin != our_tin:
        r.errors.append("შემავალი დოკუმენტის მყიდველი არ არის ჩვენი კომპანია")
    if doc.direction is Direction.OUTGOING and doc.seller.tin and doc.seller.tin != our_tin:
        r.errors.append("გამავალი დოკუმენტის გამყიდველი არ არის ჩვენი კომპანია")
    if doc.document_type is DocumentType.TAX_INVOICE and not (doc.seller.tin and doc.buyer.tin):
        r.errors.append("ანგარიშ-ფაქტურაზე ორივე მხარის საიდენტიფიკაციო კოდი სავალდებულოა")

    # --- ვალუტა და თარიღი
    if not _CURRENCY_RE.match(doc.currency):
        r.errors.append(f"ვალუტის კოდი არასწორია: {doc.currency!r}")
    if doc.issue_date is None:
        r.warnings.append("დოკუმენტის თარიღი ვერ ამოიცნო")
    elif doc.issue_date > today:
        r.errors.append(f"დოკუმენტის თარიღი მომავალშია: {doc.issue_date}")
    elif doc.issue_date < today - timedelta(days=3 * 365):
        r.warnings.append("დოკუმენტი 3 წელზე ძველია")

    # --- არითმეტიკა
    tolerance = _CENT * max(1, len(doc.line_items))
    if doc.line_items:
        lines_sum = sum((li.amount for li in doc.line_items), Decimal("0"))
        expected = doc.total_amount if doc.amounts_include_vat else doc.net_amount
        if expected is not None and not _close(lines_sum, expected, tolerance):
            r.errors.append(f"სტრიქონების ჯამი ({lines_sum}) არ ემთხვევა დოკუმენტის ჯამს ({expected})")
        for li in doc.line_items:
            if li.quantity is not None and li.unit_price is not None:
                if not _close(li.quantity * li.unit_price, li.amount, Decimal("0.05")):
                    r.warnings.append(f"'{li.description}': რაოდენობა × ფასი ≠ თანხა")

    if doc.net_amount is not None and doc.vat_amount is not None:
        if not _close(doc.net_amount + doc.vat_amount, doc.total_amount, Decimal("0.02")):
            r.errors.append("დღგ-ს გარეშე თანხა + დღგ ≠ სულ თანხა")

    # --- დღგ
    if doc.vat_treatment is VatTreatment.STANDARD_18:
        if doc.vat_amount is None:
            r.warnings.append("18% დღგ მითითებულია, მაგრამ დღგ-ს თანხა არ წერია")
        else:
            from_total = (doc.total_amount * 18 / 118).quantize(_CENT)
            from_net = (doc.net_amount * VAT_RATE).quantize(_CENT) if doc.net_amount is not None else None
            if not (_close(doc.vat_amount, from_total, Decimal("0.02"))
                    or (from_net is not None and _close(doc.vat_amount, from_net, Decimal("0.02")))):
                r.errors.append(f"დღგ-ს თანხა ({doc.vat_amount}) არ შეესაბამება 18%-ს (მოსალოდნელი ≈ {from_total})")
    elif doc.vat_treatment in (VatTreatment.EXEMPT, VatTreatment.NOT_VAT_PAYER, VatTreatment.ZERO_RATED):
        if doc.vat_amount not in (None, Decimal("0.00")):
            r.errors.append("დღგ-სგან თავისუფალ/არაგადამხდელის დოკუმენტზე დღგ-ს თანხა არ უნდა იყოს")
    elif doc.vat_treatment is VatTreatment.UNKNOWN and doc.document_type is DocumentType.TAX_INVOICE:
        r.warnings.append("ანგარიშ-ფაქტურის დღგ-ს რეჟიმი ვერ დადგინდა")

    # --- კლასიფიკაცია
    for side, code in (("დებეტის", doc.suggested_debit_account), ("კრედიტის", doc.suggested_credit_account)):
        if code is None:
            r.warnings.append(f"{side} ანგარიში არ არის შეთავაზებული")
        elif code not in chart_of_accounts:
            r.errors.append(f"{side} ანგარიში {code} არ არსებობს ანგარიშთა გეგმაში")

    r.warnings.extend(doc.warnings)
    return r
