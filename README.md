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
| ავთენტიფიკაცია (OIDC + MFA) | [`backend/app/security/oidc.py`](backend/app/security/oidc.py), [`backend/app/api/deps.py`](backend/app/api/deps.py) |
| REST API | [`backend/app/api/routes.py`](backend/app/api/routes.py) (OpenAPI: `/docs`) |
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

## API (v1)

| endpoint | როლი | რას აკეთებს |
|----------|------|-------------|
| `GET /v1/me` | ყველა | მიმდინარე მომხმარებელი, კომპანია, როლი |
| `POST/GET /v1/llm/credentials` | owner / ყველა | BYOK გასაღების დამატება (პროვაიდერთან შემოწმებით) / სია (მხოლოდ `…Xy9Z`) |
| `POST /v1/llm/credentials/{id}/default`, `DELETE /v1/llm/credentials/{id}` | owner | ნაგულისხმევის შეცვლა / გაუქმება + კრიპტო-წაშლა |
| `GET /v1/llm/usage?since=` | ყველა | LLM ხარჯი: გამოძახებები, შეცდომები, ტოკენები პროვაიდერის/მოდელის მიხედვით |
| `GET/POST /v1/consents`, `DELETE /v1/consents/{kind}` | ყველა / owner | თანხმობები (ToS, `ai_processing`, rs.ge მანდატი) ვერსიით, IP-ით, აუდიტით |
| `POST /v1/documents/extract` | owner, accountant | დოკუმენტის ამოცნობა (სჭირდება `ai_processing` თანხმობა) |
| `GET /v1/declarations`, `GET /v1/declarations/{id}` | ყველა | დეკლარაციები + სტატუსების ისტორია |
| `POST /v1/declarations/{id}/approve` | owner, accountant | დადასტურება **ნანახ** ფორმაზე (`fingerprint`) |
| `POST /v1/rsge/otp/{challenge_id}` | owner, accountant | rs.ge-ს SMS კოდი (ერთჯერადი) |
| `GET /v1/calendar/upcoming` | ყველა | საგადასახადო ვადები |
| `GET /v1/audit/verify` | owner | აუდიტის ჟურნალის ჰეშ-ჯაჭვის მთლიანობა |
| `GET /health`, `GET /ready` | - | liveness / readiness (PostgreSQL + Redis) |

ავთენტიფიკაცია: `APP_AUTH_MODE=oidc` - `Authorization: Bearer <JWT>` + `X-Tenant-Id`; როლი იკითხება
`tenant_members`-იდან, owner/accountant-ს სჭირდება MFA. dev-ში (`APP_ENV=dev` მხოლოდ) - `X-Tenant-Id`,
`X-User-Id`, არასავალდებულო `X-Role`.

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

## ტესტები და lint

```bash
cd backend
ruff check app tests                     # lint (CI-შიც ეშვება)
pytest                                   # unit + Playwright (ყალბ პორტალზე); DB/Redis ტესტები გამოტოვდება
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
TEST_REDIS_URL=redis://localhost:6379/1 pytest   # სრული ნაკრები
```

## ⚠️ production-მდე აუცილებელი

1. `backend/app/rsge/selectors.py` — eservice.rs.ge-ს რეალური სელექტორების ჩაწერა
   (`playwright codegen https://eservice.rs.ge`) ავტორიზებული სატესტო ანგარიშით.
2. `backend/app/rsge/soap.py` — ოპერაციების სიგნატურების გადამოწმება WSDL-თან.
3. `tax_calendar.py` და `ZERO_ACTIVITY_POLICY` — სერტიფიცირებული ბუღალტრის დადასტურება.
4. OIDC პროვაიდერის (Keycloak / Auth0 / Cognito) კონფიგურაცია: `APP_OIDC_*`, MFA-ს `amr` მნიშვნელობა
   (`APP_OIDC_MFA_AMR_VALUES`) — კოდი მზადაა (`app/security/oidc.py`), staging/prod dev რეჟიმით არ ეშვება.
5. იურიდიული: ToS, რწმუნებულება/მანდატი, პერსონალური მონაცემების საზღვარგარეთ გადაცემა (LLM) —
   თანხმობების ტექნიკური მხარე (`/v1/consents`) მზადაა, ტექსტები იურისტმა უნდა შეადგინოს.
6. worker-ების რიგი (ARQ/Temporal): დეკლარაციის ნაკადის (`rsge/browser.py`) მიბმა DB-სთან, შეხსენებები.
7. აუდიტის ჯაჭვის ბოლო ჰეშის პერიოდული ჩაწერა გარე WORM საცავში (`GET /v1/audit/verify` -> `last_hash`).
