"""საგადასახადო კალენდარი: დეკლარაციების ვადები + საქართველოს უქმე დღეები.

⚠️ წესები მონაცემებად არის აღწერილი (RULES), რათა ბუღალტერმა/იურისტმა შეძლოს მათი
გადამოწმება და შეცვლა კოდის ლოგიკის შეხების გარეშე. `requires_verification=True` ნიშნავს,
რომ წესი production-მდე უნდა დაადასტუროს სერტიფიცირებულმა ბუღალტერმა მოქმედ
საგადასახადო კოდექსთან და rs.ge-ს განმარტებებთან.

ვადის გადატანა: თუ ვადის ბოლო დღე არასამუშაო დღეა, ვადა გადადის მომდევნო სამუშაო დღეზე
(`shift_non_working_days`, ნაგულისხმევად ჩართულია - გადაამოწმეთ).
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from enum import Enum
from typing import Literal
from zoneinfo import ZoneInfo

from .models import DeclarationType

TBILISI = ZoneInfo("Asia/Tbilisi")


def tbilisi_today(now: datetime | None = None) -> date:
    """"დღეს" საქართველოს დროით. სერვერი, როგორც წესი, UTC-შია: 00:00-04:00 შუალედში
    `date.today()` ჯერ კიდევ გუშინდელ დღეს აბრუნებს (ვადები, "მომავალი თარიღის" შემოწმება)."""
    return (now or datetime.now(UTC)).astimezone(TBILISI).date()


class Regime(str, Enum):
    VAT_PAYER = "vat_payer"  # დღგ-ის გადამხდელი
    EMPLOYER = "employer"  # წყაროსთან დაკავება (ხელფასი, მომსახურება ფიზ. პირებისგან) + საპენსიო
    PROFIT_TAX = "profit_tax"  # მოგების გადასახადი (ესტონური მოდელი: განაწილებული მოგება)
    SMALL_BUSINESS = "small_business"  # მცირე ბიზნესის სტატუსი (1%)
    PROPERTY_TAX_ORG = "property_tax_org"  # ორგანიზაციის ქონების გადასახადი
    INDIVIDUAL_ANNUAL = "individual_annual"  # ფიზიკური პირის წლიური საშემოსავლო დეკლარაცია


@dataclass(frozen=True)
class DeadlineRule:
    code: str
    title_ka: str
    regime: Regime
    frequency: Literal["monthly", "annual"]
    due_day: int
    due_month: int | None = None  # annual: საანგარიშო წლის მომდევნო წლის თვე
    declaration_type: DeclarationType | None = None
    requires_verification: bool = False
    note_ka: str = ""


RULES: tuple[DeadlineRule, ...] = (
    DeadlineRule(
        "VAT_MONTHLY", "დღგ-ის დეკლარაცია", Regime.VAT_PAYER, "monthly", 15,
        declaration_type=DeclarationType.VAT,
        note_ka="საანგარიშო თვის მომდევნო თვის 15 რიცხვამდე; დღგ-ის გადამხდელი წარადგენს ნულოვანსაც.",
    ),
    DeadlineRule(
        "INCOME_TAX_WITHHOLDING", "საშემოსავლო (წყაროსთან დაკავებული) + საპენსიო", Regime.EMPLOYER, "monthly", 15,
        declaration_type=DeclarationType.INCOME_TAX_WITHHOLDING,
        note_ka="გაცემის თვის მომდევნო თვის 15 რიცხვამდე.",
    ),
    DeadlineRule(
        "PROFIT_TAX_MONTHLY", "მოგების გადასახადის დეკლარაცია", Regime.PROFIT_TAX, "monthly", 15,
        declaration_type=DeclarationType.PROFIT_TAX,
        note_ka="იმ თვის მომდევნო თვის 15 რიცხვამდე, როცა წარმოიშვა დაბეგვრის ობიექტი (დივიდენდი, "
                "არაეკონომიკური ხარჯი და ა.შ.).",
    ),
    DeadlineRule(
        "SMALL_BUSINESS_MONTHLY", "მცირე ბიზნესის დეკლარაცია", Regime.SMALL_BUSINESS, "monthly", 15,
        declaration_type=DeclarationType.SMALL_BUSINESS, requires_verification=True,
    ),
    DeadlineRule(
        "PROPERTY_TAX_ORG_ANNUAL", "ქონების გადასახადის წლიური დეკლარაცია (ორგანიზაცია)",
        Regime.PROPERTY_TAX_ORG, "annual", 1, due_month=4, requires_verification=True,
    ),
    DeadlineRule(
        "INDIVIDUAL_INCOME_ANNUAL", "საშემოსავლო გადასახადის წლიური დეკლარაცია (ფიზ. პირი)",
        Regime.INDIVIDUAL_ANNUAL, "annual", 31, due_month=3,
    ),
)


# ------------------------------------------------------------------ holidays


def orthodox_easter(year: int) -> date:
    """მართლმადიდებლური აღდგომა (Meeus-ის იულიუსური ალგორითმი -> გრიგორიანული, 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = (d + e + 114) % 31 + 1
    return date(year, month, day) + timedelta(days=13)


# შრომის კოდექსით დადგენილი უქმე დღეები (თვე, დღე, რომელი წლიდან). გადაამოწმეთ ყოველწლიურად -
# მთავრობა ზოგჯერ დამატებით დასვენების დღეებს აცხადებს (extra_holidays პარამეტრი).
_FIXED_HOLIDAYS: tuple[tuple[int, int, str, int], ...] = (
    (1, 1, "ახალი წელი", 1991),
    (1, 2, "ახალი წელი", 1991),
    (1, 7, "შობა", 1991),
    (1, 19, "ნათლისღება", 1991),
    (3, 3, "დედის დღე", 1991),
    (3, 8, "ქალთა საერთაშორისო დღე", 1991),
    (4, 9, "ეროვნული ერთიანობის დღე", 1991),
    (5, 9, "ფაშიზმზე გამარჯვების დღე", 1991),
    (5, 12, "წმ. ანდრია პირველწოდებულის დღე", 1991),
    (5, 17, "ოჯახის სიწმინდისა და მშობლების პატივისცემის დღე", 2024),
    (5, 26, "დამოუკიდებლობის დღე", 1991),
    (8, 28, "მარიამობა", 1991),
    (10, 14, "სვეტიცხოვლობა", 1991),
    (11, 23, "გიორგობა", 1991),
)


def georgian_holidays(year: int, extra_holidays: dict[date, str] | None = None) -> dict[date, str]:
    days = {date(year, m, d): name for m, d, name, since in _FIXED_HOLIDAYS if year >= since}
    easter = orthodox_easter(year)
    days[easter - timedelta(days=2)] = "წითელი პარასკევი"
    days[easter - timedelta(days=1)] = "დიდი შაბათი"
    days[easter] = "აღდგომა"
    days[easter + timedelta(days=1)] = "აღდგომის ორშაბათი"
    if extra_holidays:
        days.update({d: n for d, n in extra_holidays.items() if d.year == year})
    return days


class BusinessCalendar:
    def __init__(self, extra_holidays: dict[date, str] | None = None):
        self._extra = extra_holidays or {}
        self._cache: dict[int, dict[date, str]] = {}

    def holidays(self, year: int) -> dict[date, str]:
        if year not in self._cache:
            self._cache[year] = georgian_holidays(year, self._extra)
        return self._cache[year]

    def is_business_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays(d.year)

    def next_business_day(self, d: date) -> date:
        while not self.is_business_day(d):
            d += timedelta(days=1)
        return d


# ------------------------------------------------------------------ deadlines


@dataclass(frozen=True)
class TaxPeriod:
    year: int
    month: int | None = None  # None = წლიური

    def __str__(self) -> str:
        return f"{self.year}-{self.month:02d}" if self.month else str(self.year)

    @classmethod
    def parse(cls, value: str) -> TaxPeriod:
        parts = value.split("-")
        return cls(int(parts[0]), int(parts[1]) if len(parts) > 1 else None)


@dataclass(frozen=True)
class Deadline:
    rule: DeadlineRule
    period: TaxPeriod
    statutory_date: date  # კანონით (გადატანამდე)
    due_date: date  # რეალური ბოლო დღე (სამუშაო დღეზე გადატანით)

    @property
    def shifted(self) -> bool:
        return self.due_date != self.statutory_date


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    total = year * 12 + (month - 1) + n
    return total // 12, total % 12 + 1


def _clamp_day(year: int, month: int, day: int) -> date:
    """31 რიცხვი თებერვალში -> თვის ბოლო დღე."""
    return date(year, month, min(day, monthrange(year, month)[1]))


def deadline_for(rule: DeadlineRule, period: TaxPeriod, calendar: BusinessCalendar | None = None,
                 *, shift_non_working_days: bool = True) -> Deadline:
    calendar = calendar or BusinessCalendar()
    if rule.frequency == "monthly":
        if period.month is None:
            raise ValueError(f"{rule.code} needs a monthly period")
        y, m = _add_months(period.year, period.month, 1)
        statutory = _clamp_day(y, m, rule.due_day)
    else:
        statutory = _clamp_day(period.year + 1, rule.due_month or 12, rule.due_day)
    due = calendar.next_business_day(statutory) if shift_non_working_days else statutory
    return Deadline(rule, period, statutory, due)


def upcoming_deadlines(
    today: date,
    regimes: set[Regime],
    *,
    horizon_days: int = 45,
    calendar: BusinessCalendar | None = None,
    rules: tuple[DeadlineRule, ...] = RULES,
) -> list[Deadline]:
    """ვადები [today, today + horizon] შუალედში, თარიღით დალაგებული."""
    calendar = calendar or BusinessCalendar()
    end = today + timedelta(days=horizon_days)
    found: list[Deadline] = []
    for rule in rules:
        if rule.regime not in regimes:
            continue
        if rule.frequency == "monthly":
            months_back = 2
            months_ahead = horizon_days // 28 + 1
            candidates = [TaxPeriod(*_add_months(today.year, today.month, k))
                          for k in range(-months_back, months_ahead)]
        else:
            candidates = [TaxPeriod(y) for y in (today.year - 2, today.year - 1, today.year)]
        for period in candidates:
            dl = deadline_for(rule, period, calendar)
            if today <= dl.due_date <= end:
                found.append(dl)
    return sorted(found, key=lambda d: (d.due_date, d.rule.code))


def reminder_dates(deadline: Deadline, today: date, offsets_days: tuple[int, ...] = (7, 3, 1, 0)) -> list[date]:
    """შეხსენებები ვადამდე N დღით ადრე (წარსულში არა). 0 = ვადის დღე."""
    dates = {deadline.due_date - timedelta(days=o) for o in offsets_days}
    return sorted(d for d in dates if d >= today)
