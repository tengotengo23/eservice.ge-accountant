"""ბიზნესის მფლობელისთვის ყოველთვიური ანგარიში მარტივ ენაზე.

ციფრებს ითვლის კოდი (MonthlySummary), LLM მხოლოდ "ყვება". შემდეგ ვამოწმებთ, რომ ტექსტში
არ გაჩნდა ციფრი, რომელიც ფაქტებში არ იყო (ჰალუცინაციის დაცვა).
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from ..llm import Effort, LLMCredentials, LLMRouter
from .models import Money
from .tax_calendar import tbilisi_today


class MonthlySummary(BaseModel):
    company_name: str
    period: str
    revenue: Money
    expenses: Money
    output_vat: Money
    input_vat: Money
    cash_balance: Money
    documents_needing_review: int
    upcoming_deadlines: list[str] = Field(description="მაგ. '2026-10-15 - დღგ-ის დეკლარაცია (სექტემბერი)'")

    @property
    def profit_before_tax(self) -> Decimal:
        return self.revenue - self.expenses

    @property
    def vat_payable(self) -> Decimal:
        return self.output_vat - self.input_vat


class OwnerReport(BaseModel):
    headline: str = Field(description="ერთი წინადადება - მთავარი სიახლე")
    paragraphs: list[str] = Field(description="2-4 მოკლე აბზაცი, ჟარგონის გარეშე")
    action_items: list[str] = Field(description="რა უნდა გააკეთოს მფლობელმა (კონკრეტული ნაბიჯები)")
    risks: list[str]

    def full_text(self) -> str:
        return "\n".join([self.headline, *self.paragraphs, *self.action_items, *self.risks])


REPORT_SYSTEM_PROMPT = """\
შენ წერ ყოველთვიურ ფინანსურ მიმოხილვას მცირე ბიზნესის მფლობელისთვის, რომელიც ბუღალტერი არ არის.
- წერე მარტივ ქართულ ენაზე, მოკლედ, ბუღალტრული ჟარგონის გარეშე (ან აუხსენი).
- გამოიყენე მხოლოდ მოცემული ციფრები. არ გამოთვალო ახალი ციფრები, არ დაამრგვალო, არ გამოთქვა პროცენტები.
- თანხები ჩაწერე ზუსტად ისე, როგორც მოცემულია, "₾" სიმბოლოთი.
- action_items - კონკრეტული ქმედებები ვადებით (თუ ვადა მოცემულია).
"""

# ათასების გამყოფი (space / NBSP / narrow NBSP / მძიმე) მხოლოდ 3-ციფრიან ჯგუფებს შორის: "12,500.00", "9 000".
# ასე "2, 3 და 4" ან ორ სტრიქონზე მდგომი ციფრები ერთ რიცხვად ("23") აღარ ერწყმის ერთმანეთს.
_NUMBER_RE = re.compile(r"(?<![\w.,])-?(?:\d{1,3}(?:[ \u00a0\u202f,]\d{3})+|\d+)(?:\.\d+)?%?(?!\d)")


def _normalize_number(raw: str) -> Decimal | None:
    cleaned = raw.replace(" ", "").replace("\u00a0", "").replace("\u202f", "").replace(",", "")
    try:
        return Decimal(cleaned)
    except ArithmeticError:
        return None


def allowed_numbers(summary: MonthlySummary) -> set[Decimal]:
    facts = summary.model_dump(mode="json")
    facts["profit_before_tax"] = str(summary.profit_before_tax)
    facts["vat_payable"] = str(summary.vat_payable)
    text = json.dumps(facts, ensure_ascii=False)
    numbers = {n for raw in _NUMBER_RE.findall(text) if (n := _normalize_number(raw)) is not None}
    # თარიღების ნაწილები (2026-10-15 -> 2026, 10, 15) და პატარა რიგითი რიცხვები ("2 ნაბიჯი")
    for part in re.findall(r"\d+", text):
        numbers.add(Decimal(part))
    numbers.update(Decimal(i) for i in range(0, 13))
    return {n.normalize() for n in numbers} | {abs(n).normalize() for n in numbers}


def unsupported_numbers(report: OwnerReport, summary: MonthlySummary) -> list[str]:
    allowed = allowed_numbers(summary)
    bad = []
    for raw in _NUMBER_RE.findall(report.full_text()):
        if raw.endswith("%"):  # პროცენტებს ფაქტები არ შეიცავს - LLM-მა თავად გამოთვალა
            bad.append(raw.strip())
            continue
        value = _normalize_number(raw.strip())
        if value is not None and value.normalize() not in allowed:
            bad.append(raw.strip())
    return bad


async def generate_owner_report(router: LLMRouter, creds: LLMCredentials, summary: MonthlySummary,
                                today: date | None = None) -> tuple[OwnerReport, list[str]]:
    facts = summary.model_dump(mode="json")
    facts["profit_before_tax"] = str(summary.profit_before_tax)
    facts["vat_payable_estimate"] = str(summary.vat_payable)
    prompt = (f"დღეს: {(today or tbilisi_today()).isoformat()}\n"
              f"ფაქტები:\n{json.dumps(facts, ensure_ascii=False, indent=2)}")
    result = await router.structured(creds, system=REPORT_SYSTEM_PROMPT, prompt=prompt,
                                     output_model=OwnerReport, effort=Effort.LOW, max_output_tokens=4_000)
    return result.data, unsupported_numbers(result.data, summary)
