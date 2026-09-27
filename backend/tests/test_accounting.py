import io
import json
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook

from app.accounting.ai_accountant import AIAccountant, merge_decisions, rule_based_decision
from app.accounting.documents import UnsupportedDocumentError, to_attachment, xlsx_to_csv_text
from app.accounting.models import (
    DeclarationAction,
    DeclarationDecision,
    DeclarationType,
    ExtractedDocument,
    PeriodFacts,
)
from app.accounting.reporting import MonthlySummary, OwnerReport, unsupported_numbers
from app.accounting.tax_calendar import (
    RULES,
    BusinessCalendar,
    Regime,
    TaxPeriod,
    deadline_for,
    orthodox_easter,
    reminder_dates,
    upcoming_deadlines,
)
from app.accounting.validation import validate_extracted_document
from app.llm import LLMCredentials, LLMUnavailableError, Provider
from app.llm.base import RawCompletion, Usage
from app.llm.router import LLMRouter
from tests.test_llm_router import VALID_DOC

VAT_RULE = next(r for r in RULES if r.code == "VAT_MONTHLY")
TODAY = date(2026, 9, 27)
OUR_TIN = "405123456"


# ------------------------------------------------------------------ calendar


@pytest.mark.parametrize("year,expected", [(2024, date(2024, 5, 5)), (2025, date(2025, 4, 20)),
                                           (2026, date(2026, 4, 12)), (2027, date(2027, 5, 2))])
def test_orthodox_easter(year, expected):
    assert orthodox_easter(year) == expected


@pytest.mark.parametrize("period,due", [
    ("2026-02", date(2026, 3, 16)),  # 15 მარტი კვირაა
    ("2026-03", date(2026, 4, 15)),  # ჩვეულებრივი ოთხშაბათი
    ("2026-10", date(2026, 11, 16)),  # 15 ნოემბერი კვირაა
    ("2027-04", date(2027, 5, 18)),  # 15 შაბ -> 16 კვ -> 17 მაისი უქმეა -> სამშაბათი
])
def test_vat_deadline_shifts_to_next_business_day(period, due):
    assert deadline_for(VAT_RULE, TaxPeriod.parse(period)).due_date == due


def test_holidays_and_extra_days_off():
    cal = BusinessCalendar(extra_holidays={date(2026, 10, 15): "მთავრობის დადგენილებით"})
    assert not cal.is_business_day(date(2026, 4, 10))  # წითელი პარასკევი 2026
    assert deadline_for(VAT_RULE, TaxPeriod(2026, 9), cal).due_date == date(2026, 10, 16)


def test_upcoming_deadlines_and_reminders():
    items = upcoming_deadlines(TODAY, {Regime.VAT_PAYER, Regime.EMPLOYER}, horizon_days=30)
    assert [(i.rule.code, str(i.period), i.due_date) for i in items] == [
        ("INCOME_TAX_WITHHOLDING", "2026-09", date(2026, 10, 15)),
        ("VAT_MONTHLY", "2026-09", date(2026, 10, 15)),
    ]
    assert reminder_dates(items[0], TODAY) == [date(2026, 10, 8), date(2026, 10, 12), date(2026, 10, 14),
                                               date(2026, 10, 15)]


# ------------------------------------------------------------------ validation


def _doc(**overrides) -> ExtractedDocument:
    data = json.loads(json.dumps(VALID_DOC))
    data.update(overrides)
    return ExtractedDocument.model_validate(data)


def test_valid_invoice_passes():
    report = validate_extracted_document(_doc(), our_tin=OUR_TIN, today=TODAY)
    assert report.ok, report.errors


def test_wrong_vat_and_foreign_buyer_are_errors():
    doc = _doc(vat_amount=25.0, net_amount=93.0, buyer={"name": "სხვა", "tin": "999999999"})
    errors = validate_extracted_document(doc, our_tin=OUR_TIN, today=TODAY).errors
    assert any("18%" in e for e in errors)
    assert any("მყიდველი არ არის ჩვენი" in e for e in errors)


def test_bad_tin_future_date_and_unknown_account():
    doc = _doc(seller={"name": "x", "tin": "12AB"}, issue_date="2027-01-01", suggested_debit_account="9999")
    errors = validate_extracted_document(doc, our_tin=OUR_TIN, today=TODAY).errors
    assert len(errors) == 3


# ------------------------------------------------------------------ documents


def test_xlsx_bank_statement_becomes_csv_text():
    wb = Workbook()
    ws = wb.active
    ws.append(["თარიღი", "დანიშნულება", "თანხა"])
    ws.append(["2026-09-01", "იჯარის გადახდა", -1500])
    buf = io.BytesIO()
    wb.save(buf)
    att = to_attachment(buf.getvalue(), "statement.xlsx")
    assert att.media_type == "text/csv" and att.filename == "statement.csv"
    assert "იჯარის გადახდა" in att.as_text()


def test_unknown_binary_is_rejected():
    with pytest.raises(UnsupportedDocumentError):
        to_attachment(b"\x00\x01binary", "file.exe")


def test_gif_is_detected_and_legacy_xls_gets_a_clear_error():
    assert to_attachment(b"GIF89a....", "scan.gif").media_type == "image/gif"
    with pytest.raises(UnsupportedDocumentError, match="xlsx"):
        to_attachment(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest", "old.xls")


def test_xlsx_expansion_is_capped():
    wb = Workbook()
    for s in range(3):
        ws = wb.create_sheet(f"s{s}")
        for _ in range(200):
            ws.append(["x" * 200] * 5)
    buf = io.BytesIO()
    wb.save(buf)
    assert len(xlsx_to_csv_text(buf.getvalue())) > 10_000
    with pytest.raises(UnsupportedDocumentError, match="დიდია"):
        xlsx_to_csv_text(buf.getvalue(), max_chars=10_000)


def test_corrupt_xlsx_is_rejected_cleanly():
    with pytest.raises(UnsupportedDocumentError):
        to_attachment(b"PK\x03\x04 not really a zip", "broken.xlsx")


# ------------------------------------------------------------------ declarations


def facts(**kw) -> PeriodFacts:
    base = dict(taxpayer_tin=OUR_TIN, period="2026-09", is_vat_payer=True, has_employees=False)
    base.update(kw)
    return PeriodFacts(**base)


def test_rules_zero_vat_only_when_no_activity():
    assert rule_based_decision(facts(), DeclarationType.VAT).action is DeclarationAction.FILE_ZERO
    assert rule_based_decision(facts(sales_invoices_count=1), DeclarationType.VAT).action \
        is DeclarationAction.FILE_WITH_DATA
    assert rule_based_decision(facts(bank_inflows_unexplained_total=Decimal("500")), DeclarationType.VAT).action \
        is DeclarationAction.NEEDS_HUMAN_REVIEW
    assert rule_based_decision(facts(is_vat_payer=False), DeclarationType.VAT).action \
        is DeclarationAction.NOT_REQUIRED
    prefilled = facts(portal_prefilled_nonzero_fields=["სტრ. 11 - ჩასათვლელი დღგ"])
    assert rule_based_decision(prefilled, DeclarationType.VAT).action is DeclarationAction.FILE_WITH_DATA


def test_rules_employer_without_salary_needs_review():
    d = rule_based_decision(facts(has_employees=True), DeclarationType.INCOME_TAX_WITHHOLDING)
    assert d.action is DeclarationAction.NEEDS_HUMAN_REVIEW


def _llm(action, confidence=0.95):
    return DeclarationDecision(declaration_type=DeclarationType.VAT, period="2026-09", action=action,
                               reasoning_ka="...", blocking_issues=[], confidence=confidence)


def test_merge_requires_agreement_and_confidence():
    rule = rule_based_decision(facts(), DeclarationType.VAT)
    assert merge_decisions(rule, _llm(DeclarationAction.FILE_ZERO), threshold=0.9).auto_file_zero_allowed
    assert merge_decisions(rule, _llm(DeclarationAction.FILE_WITH_DATA), threshold=0.9).action \
        is DeclarationAction.NEEDS_HUMAN_REVIEW
    assert merge_decisions(rule, _llm(DeclarationAction.FILE_ZERO, 0.6), threshold=0.9).action \
        is DeclarationAction.NEEDS_HUMAN_REVIEW
    assert merge_decisions(rule, None, threshold=0.9).action is DeclarationAction.NEEDS_HUMAN_REVIEW


def test_merge_rejects_llm_answer_about_another_period_or_with_blocking_issues():
    rule = rule_based_decision(facts(), DeclarationType.VAT)
    wrong_period = _llm(DeclarationAction.FILE_ZERO).model_copy(update={"period": "2026-08"})
    merged = merge_decisions(rule, wrong_period, threshold=0.9)
    assert merged.action is DeclarationAction.NEEDS_HUMAN_REVIEW and "2026-08" in merged.reasons[0]
    wrong_type = _llm(DeclarationAction.FILE_ZERO).model_copy(
        update={"declaration_type": DeclarationType.PROFIT_TAX})
    assert merge_decisions(rule, wrong_type, threshold=0.9).action is DeclarationAction.NEEDS_HUMAN_REVIEW
    hedged = _llm(DeclarationAction.FILE_ZERO).model_copy(update={"blocking_issues": ["ბანკში უცნობი ჩარიცხვა"]})
    merged = merge_decisions(rule, hedged, threshold=0.9)
    assert merged.action is DeclarationAction.NEEDS_HUMAN_REVIEW and "ბანკში" in merged.reasons[0]


class FixedAdapter:
    provider = Provider.CLAUDE

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def generate_json(self, **kw):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return RawCompletion(self.reply, "claude-opus-5", Usage(1, 1))

    async def list_models(self, api_key):
        return []


async def test_ai_accountant_process_document_end_to_end():
    adapter = FixedAdapter(json.dumps(VALID_DOC))
    accountant = AIAccountant(LLMRouter({Provider.CLAUDE: adapter}), auto_approve_confidence=0.9)
    out = await accountant.process_document(LLMCredentials(Provider.CLAUDE, "k"), b"%PDF-1.7 ...", "a.pdf",
                                            our_tin=OUR_TIN, today=TODAY)
    assert out.validation.ok and not out.needs_review


async def test_ai_accountant_decision_falls_back_to_human_when_llm_down():
    adapter = FixedAdapter(LLMUnavailableError("down"))
    accountant = AIAccountant(LLMRouter({Provider.CLAUDE: adapter}, transient_retries=0), auto_approve_confidence=0.9)
    decision = await accountant.decide_declaration(LLMCredentials(Provider.CLAUDE, "k"), facts(), DeclarationType.VAT)
    assert decision.action is DeclarationAction.NEEDS_HUMAN_REVIEW


async def test_ai_accountant_skips_llm_for_clear_cases():
    adapter = FixedAdapter("{}")
    accountant = AIAccountant(LLMRouter({Provider.CLAUDE: adapter}), auto_approve_confidence=0.9)
    decision = await accountant.decide_declaration(LLMCredentials(Provider.CLAUDE, "k"),
                                                   facts(sales_total=Decimal("100")), DeclarationType.VAT)
    assert decision.action is DeclarationAction.FILE_WITH_DATA and adapter.calls == 0


# ------------------------------------------------------------------ reporting


def test_report_number_guard_flags_invented_numbers():
    summary = MonthlySummary(company_name="შპს ტესტი", period="2026-09", revenue=Decimal("12500"),
                             expenses=Decimal("8300.50"), output_vat=Decimal("1906.78"), input_vat=Decimal("1100"),
                             cash_balance=Decimal("40210.15"), documents_needing_review=2,
                             upcoming_deadlines=["2026-10-15 - დღგ-ის დეკლარაცია"])
    good = OwnerReport(headline="სექტემბერში შემოსავალი 12,500.00 ₾ იყო",
                       paragraphs=["მოგება დაბეგვრამდე: 4199.50 ₾", "დღგ გადასახდელი ≈ 806.78 ₾"],
                       action_items=["2026-10-15-მდე წარადგინეთ დღგ-ის დეკლარაცია", "განიხილეთ 2 დოკუმენტი"],
                       risks=[])
    assert unsupported_numbers(good, summary) == []
    bad = good.model_copy(update={"risks": ["ხარჯები 15%-ით გაიზარდა, ~9 000 ₾"]})
    assert unsupported_numbers(bad, summary) == ["15%", "9 000"]


def test_report_number_guard_does_not_glue_separate_numbers():
    summary = MonthlySummary(company_name="შპს ტესტი", period="2026-09", revenue=Decimal("1234567.89"),
                             expenses=Decimal("0"), output_vat=Decimal("0"), input_vat=Decimal("0"),
                             cash_balance=Decimal("0"), documents_needing_review=3, upcoming_deadlines=[])
    report = OwnerReport(headline="შემოსავალი: 1 234 567.89 ₾",
                         paragraphs=["განიხილეთ 2, 3 და 4 ნაბიჯი", "დოკუმენტები: 3\n4 ვადა"],
                         action_items=[], risks=[])
    assert unsupported_numbers(report, summary) == []
