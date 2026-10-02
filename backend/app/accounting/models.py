"""ბუღალტრული დომენის მოდელები. იგივე მოდელები გამოიყენება LLM-ის structured output-ის სქემად."""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, WithJsonSchema


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# თანხები: Decimal (არასდროს float), 2 ათწილადი. LLM-ისთვის სქემაში - უბრალოდ "number".
Money = Annotated[Decimal, WithJsonSchema({"type": "number"}), AfterValidator(_q2)]
Quantity = Annotated[Decimal, WithJsonSchema({"type": "number"})]
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class DocumentType(str, Enum):
    TAX_INVOICE = "tax_invoice"  # საგადასახადო ანგარიშ-ფაქტურა
    WAYBILL = "waybill"  # სასაქონლო ზედნადები
    INVOICE = "invoice"  # ინვოისი / ანგარიში გადასახდელად
    RECEIPT = "receipt"  # სალაროს ჩეკი / ქვითარი
    BANK_STATEMENT = "bank_statement"  # საბანკო ამონაწერი
    ACCEPTANCE_ACT = "acceptance_act"  # მიღება-ჩაბარების აქტი
    CONTRACT = "contract"
    PAYROLL = "payroll"  # სახელფასო უწყისი
    OTHER = "other"


class Direction(str, Enum):
    INCOMING = "incoming"  # შესყიდვა / ხარჯი (ჩვენ ვართ მყიდველი)
    OUTGOING = "outgoing"  # რეალიზაცია / შემოსავალი (ჩვენ ვართ გამყიდველი)
    NOT_APPLICABLE = "not_applicable"


class VatTreatment(str, Enum):
    STANDARD_18 = "standard_18"  # 18% დღგ
    ZERO_RATED = "zero_rated"  # 0% (ექსპორტი და ა.შ.)
    EXEMPT = "exempt"  # დღგ-სგან გათავისუფლებული
    NOT_VAT_PAYER = "not_vat_payer"  # გამყიდველი არ არის დღგ-ის გადამხდელი
    UNKNOWN = "unknown"


class Counterparty(BaseModel):
    name: str | None = Field(description="დასახელება ან სახელი-გვარი, როგორც დოკუმენტშია")
    tin: str | None = Field(
        description="საიდენტიფიკაციო კოდი (9 ციფრი, იურიდიული პირი) ან პირადი ნომერი (11 ციფრი). მხოლოდ ციფრები."
    )


class LineItem(BaseModel):
    description: str
    quantity: Quantity | None = Field(description="რაოდენობა")
    unit_price: Money | None
    amount: Money = Field(description="სტრიქონის ჯამი დოკუმენტში მითითებული სახით")


class ExtractedDocument(BaseModel):
    """ფინანსური დოკუმენტიდან ამოღებული მონაცემები + ბუღალტრული კლასიფიკაციის შეთავაზება."""

    document_type: DocumentType
    direction: Direction
    document_number: str | None
    issue_date: date | None = Field(description="ISO ფორმატი YYYY-MM-DD")
    currency: str = Field(description="ISO 4217, მაგ. GEL, USD, EUR")
    seller: Counterparty
    buyer: Counterparty
    line_items: list[LineItem]
    amounts_include_vat: bool = Field(description="სტრიქონების თანხები დღგ-ს ჩათვლითაა თუ არა")
    net_amount: Money | None = Field(description="თანხა დღგ-ს გარეშე")
    vat_amount: Money | None = Field(description="დღგ-ს თანხა; თუ დოკუმენტში არ წერია - null")
    total_amount: Money = Field(description="სულ გადასახდელი თანხა")
    vat_treatment: VatTreatment
    suggested_debit_account: str | None = Field(description="ანგარიშთა გეგმის კოდი (დებეტი), მოცემული სიიდან")
    suggested_credit_account: str | None = Field(description="ანგარიშთა გეგმის კოდი (კრედიტი), მოცემული სიიდან")
    classification_rationale: str = Field(description="მოკლე განმარტება ქართულად, რატომ ეს ანგარიშები")
    confidence: Confidence = Field(description="0..1 - რამდენად დარწმუნებულია მოდელი ამოღებაში")
    warnings: list[str] = Field(description="გაურკვეველი/ბუნდოვანი ადგილები, ქართულად")


class DeclarationType(str, Enum):
    VAT = "vat"  # დღგ-ის დეკლარაცია (ყოველთვიური)
    INCOME_TAX_WITHHOLDING = "income_tax_withholding"  # საშემოსავლო (წყაროსთან დაკავებული) + საპენსიო
    PROFIT_TAX = "profit_tax"  # მოგების გადასახადი (განაწილებული მოგება)
    SMALL_BUSINESS = "small_business"  # მცირე ბიზნესის (1%) დეკლარაცია


class DeclarationAction(str, Enum):
    FILE_ZERO = "file_zero"
    FILE_WITH_DATA = "file_with_data"
    NOT_REQUIRED = "not_required"
    NEEDS_HUMAN_REVIEW = "needs_human_review"


class PeriodFacts(BaseModel):
    """დეტერმინისტულად შეგროვებული ფაქტები პერიოდზე (DB + rs.ge სინქრონიზაცია). LLM არ ითვლის."""

    taxpayer_tin: str
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    is_vat_payer: bool
    has_employees: bool
    sales_invoices_count: int = 0
    sales_total: Decimal = Decimal("0")
    purchase_invoices_count: int = 0
    purchase_vat_total: Decimal = Decimal("0")
    outgoing_waybills_count: int = 0
    bank_inflows_total: Decimal = Decimal("0")
    # შემოსავალი, რომელიც სესხად/კაპიტალად არ არის კლასიფიცირებული
    bank_inflows_unexplained_total: Decimal = Decimal("0")
    salary_payments_total: Decimal = Decimal("0")
    other_withholding_payments_total: Decimal = Decimal("0")  # მომსახურება ფიზ. პირებისგან და ა.შ.
    dividends_paid_total: Decimal = Decimal("0")
    non_business_expenses_total: Decimal = Decimal("0")  # მოგების გადასახადის ობიექტი ესტონურ მოდელში
    portal_prefilled_nonzero_fields: list[str] = Field(
        default_factory=list, description="rs.ge ფორმაში ავტომატურად შევსებული არანულოვანი ველები"
    )


class DeclarationDecision(BaseModel):
    declaration_type: DeclarationType
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="საანგარიშო პერიოდი, YYYY-MM")
    action: DeclarationAction
    reasoning_ka: str = Field(description="ახსნა ქართულად, მარტივ ენაზე, მეწარმისთვის")
    blocking_issues: list[str] = Field(description="რა უშლის ხელს ავტომატურ წარდგენას")
    confidence: Confidence
