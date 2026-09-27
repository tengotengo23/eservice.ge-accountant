# AI-ბუღალტერი (eservice.rs.ge ავტომატიზაცია)

SaaS პლატფორმის ბირთვი, რომელიც ანაცვლებს რუტინულ ბუღალტრულ სამუშაოს:
დოკუმენტების ამოცნობა (PDF/JPG/Excel) LLM-ით, საგადასახადო კალენდარი, ნულოვანი დეკლარაციების
მომზადება და წარდგენა eservice.rs.ge-ზე — **მომხმარებლის საკუთარი** Claude / ChatGPT / Gemini გასაღებით.

📐 სრული არქიტექტურა, უსაფრთხოება და რისკები: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**

## რა არის აქ

| მოთხოვნა | სად |
|----------|-----|
| სისტემის არქიტექტურა, სტეკი, უსაფრთხოება | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| rs.ge ავტომატიზაცია (Playwright, 2FA, ნულოვანი დეკლარაცია) | [`backend/app/rsge/browser.py`](backend/app/rsge/browser.py), [`otp.py`](backend/app/rsge/otp.py), [`selectors.py`](backend/app/rsge/selectors.py) |
| LLM Router (Claude / ChatGPT / Gemini) | [`backend/app/llm/router.py`](backend/app/llm/router.py), [`providers/`](backend/app/llm/providers) |
| AI ბუღალტრის ლოგიკა | [`backend/app/accounting/ai_accountant.py`](backend/app/accounting/ai_accountant.py), [`validation.py`](backend/app/accounting/validation.py), [`tax_calendar.py`](backend/app/accounting/tax_calendar.py), [`reporting.py`](backend/app/accounting/reporting.py) |
| მონაცემთა ბაზის სქემა | [`backend/db/schema.sql`](backend/db/schema.sql) |
| საიდუმლოებების დაშიფვრა | [`backend/app/security/crypto.py`](backend/app/security/crypto.py) |
| რისკები (2FA, CAPTCHA, პასუხისმგებლობა) | [`docs/ARCHITECTURE.md#6-რისკები-და-შეზღუდვები`](docs/ARCHITECTURE.md) |

## მაგალითი: დოკუმენტის დამუშავება არჩეული პროვაიდერით

```python
from app.llm import LLMCredentials, LLMRouter, Provider
from app.accounting.ai_accountant import AIAccountant

accountant = AIAccountant(LLMRouter())
creds = LLMCredentials(Provider.CLAUDE, api_key="sk-ant-...", model="claude-opus-5")
# ან: LLMCredentials(Provider.CHATGPT, "sk-proj-...", "gpt-6-sol")
# ან: LLMCredentials(Provider.GEMINI, "AIza...", "gemini-3.8-flash")

result = await accountant.process_document(creds, open("invoice.pdf", "rb").read(), "invoice.pdf",
                                           our_tin="405123456")
result.extraction.total_amount, result.extraction.vat_amount   # Decimal
result.validation.errors                                       # დეტერმინისტული შემოწმებები
result.needs_review                                            # -> ბუღალტრის რიგი
```

## გაშვება (dev)

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

# KEK dev-ისთვის (prod-ში: APP_KEY_PROVIDER=aws-kms)
export APP_LOCAL_MASTER_KEY=$(python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())")

docker compose up -d postgres redis        # რეპოზიტორიის ძირიდან
uvicorn app.main:app --reload
```

## ტესტები

```bash
cd backend
pytest                                   # unit + Playwright (ყალბ პორტალზე); DB/Redis ტესტები გამოტოვდება
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
TEST_REDIS_URL=redis://localhost:6379/1 pytest   # სრული ნაკრები
```

## ⚠️ production-მდე აუცილებელი

1. `backend/app/rsge/selectors.py` — eservice.rs.ge-ს რეალური სელექტორების ჩაწერა
   (`playwright codegen https://eservice.rs.ge`) ავტორიზებული სატესტო ანგარიშით.
2. `backend/app/rsge/soap.py` — ოპერაციების სიგნატურების გადამოწმება WSDL-თან.
3. `tax_calendar.py` და `ZERO_ACTIVITY_POLICY` — სერტიფიცირებული ბუღალტრის დადასტურება.
4. OIDC ავთენტიფიკაცია (`app/api/deps.py`) — ახლა მხოლოდ dev header-ებია.
5. იურიდიული: ToS, რწმუნებულება/მანდატი, პერსონალური მონაცემების საზღვარგარეთ გადაცემა (LLM).
