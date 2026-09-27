"""rs.ge პორტალის სელექტორები და მისამართები - ერთ ადგილას.

⚠️ ეს მნიშვნელობები შაბლონია. eservice.rs.ge-ს რეალური DOM უნდა ჩაიწეროს ავტორიზებული
მომხმარებლის მიერ `playwright codegen https://eservice.rs.ge` ბრძანებით და ჩანაცვლდეს აქ
(ან DB/YAML კონფიგურაციაში, რათა ინტერფეისის ცვლილებაზე deploy არ დაგვჭირდეს).
პორტალი პერიოდულად იცვლება: ყოველდღიური "canary" ტესტი (მხოლოდ login + ნავიგაცია, წარდგენის
გარეშე) ადრევე აჩვენებს, თუ რომელიმე სელექტორი გაფუჭდა.

სელექტორები Playwright-ის CSS-ია (`:has-text()` გაფართოებით). უპირატესობა მიანიჭეთ
სტაბილურ ატრიბუტებს (name, aria-label, data-*) და არა CSS კლასებს/მდებარეობას.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..accounting.models import DeclarationType


@dataclass(frozen=True)
class RsGeSelectors:
    # --- მისამართები (base_url-ის მიმართ)
    login_path: str = "/login"
    home_path: str = "/dashboard"
    declarations_path: str = "/declarations"

    # --- ავტორიზაცია
    username_input: str = "input[name='username']"
    password_input: str = "input[type='password']"  # noqa: S105 - CSS სელექტორი, არა პაროლი
    login_button: str = "button:has-text('შესვლა')"
    login_error: str = ".alert-danger"
    captcha_marker: str = "iframe[src*='captcha'], .g-recaptcha, [data-captcha], img.captcha"
    otp_input: str = "input[autocomplete='one-time-code'], input[name='otp']"
    otp_submit: str = "button:has-text('დადასტურება')"
    otp_error: str = ".otp-error"
    logged_in_marker: str = "a:has-text('გასვლა')"
    forced_password_change_marker: str = "form.change-password"  # noqa: S105

    # --- დეკლარაციები
    declarations_rows: str = "table.declarations tbody tr"
    row_open_link: str = "a:has-text('გახსნა')"
    status_submitted_text: str = "წარდგენილი"
    status_draft_text: str = "დრაფტი"
    new_declaration_button: str = "a:has-text('ახალი დეკლარაცია')"
    declaration_type_select: str = "select[name='declaration_type']"
    period_year_select: str = "select[name='year']"
    period_month_select: str = "select[name='month']"
    continue_button: str = "button:has-text('გაგრძელება')"

    # --- დეკლარაციის ფორმა
    form_root: str = "form.declaration-form"
    amount_inputs: str = "form.declaration-form input[type='number']"
    fill_empty_with_zero: bool = True
    save_draft_button: str = "button:has-text('შენახვა')"
    draft_saved_marker: str = ".draft-saved"
    submit_button: str = "button:has-text('გაგზავნა')"
    submit_confirm_text: str = "გაგზავნა"  # native confirm() დიალოგის ტექსტში უნდა იყოს
    submission_success: str = ".submission-success"
    receipt_number: str = ".submission-success .receipt-number"
    submission_error: str = ".submission-error"

    # ფორმის ჩამოსაშლელ სიაში დეკლარაციის ტიპის ტექსტი
    declaration_type_labels: dict[DeclarationType, str] = field(default_factory=lambda: {
        DeclarationType.VAT: "დღგ",
        DeclarationType.INCOME_TAX_WITHHOLDING: "საშემოსავლო (წყაროსთან)",
        DeclarationType.PROFIT_TAX: "მოგების გადასახადი",
        DeclarationType.SMALL_BUSINESS: "მცირე ბიზნესი",
    })


DEFAULT_SELECTORS = RsGeSelectors()
