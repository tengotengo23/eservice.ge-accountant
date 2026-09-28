-- =====================================================================
-- AI-ბუღალტერი - PostgreSQL 15+ სქემა (საბაზისო მიგრაცია)
--
-- მიგრაციებს უშვებს `python -m app.db.migrate` (თითო ფაილი - ერთი ტრანზაქცია, ამიტომ BEGIN/COMMIT
-- აქ არ წერია). გამოყენებული მიგრაცია აღარ იცვლება: ცვლილება = ახალი ფაილი (0002_..., 0003_...).
--
-- * Multi-tenant: ყველა ბიზნეს-ცხრილს აქვს tenant_id + Row Level Security.
--   აპლიკაცია ყოველ ტრანზაქციაში აკეთებს:  SELECT set_config('app.tenant_id', '<uuid>', true);
--   და უკავშირდება როლით ai_accountant_app (არა superuser/owner - მათზე RLS არ მოქმედებს).
-- * საიდუმლოებები (API Keys, rs.ge პაროლები, სესიები) ინახება მხოლოდ დაშიფრულად
--   (envelope encryption: ciphertext + wrapped_dek + kek_id). plaintext DB-ში არასდროს ხვდება.
-- * თანხები: numeric(18,2), არასდროს float.
-- * აუდიტი: append-only, hash-chain (ცვლილება/წაშლა შეუძლებელია, ჩანაწერის ამოღება ჯაჭვს წყვეტს).
-- =====================================================================


DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_accountant_app') THEN
        CREATE ROLE ai_accountant_app NOLOGIN;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION app_current_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;

-- ------------------------------------------------------------------ enums

CREATE TYPE member_role        AS ENUM ('owner', 'accountant', 'viewer');
CREATE TYPE llm_provider       AS ENUM ('claude', 'chatgpt', 'gemini');
CREATE TYPE legal_form         AS ENUM ('llc', 'jsc', 'individual_entrepreneur', 'individual', 'ngo', 'branch', 'other');
CREATE TYPE tax_regime         AS ENUM ('vat_payer', 'employer', 'profit_tax', 'small_business',
                                        'property_tax_org', 'individual_annual');
CREATE TYPE rsge_credential_kind AS ENUM ('portal_user', 'service_user');
CREATE TYPE credential_status  AS ENUM ('active', 'invalid', 'locked', 'revoked');
CREATE TYPE document_source    AS ENUM ('upload', 'email', 'rsge_invoice', 'rsge_waybill', 'bank_import');
CREATE TYPE document_status    AS ENUM ('uploaded', 'processing', 'extracted', 'needs_review', 'approved',
                                        'rejected', 'failed');
CREATE TYPE account_type       AS ENUM ('asset', 'liability', 'equity', 'revenue', 'expense');
CREATE TYPE entry_status       AS ENUM ('draft', 'posted', 'reversed');
CREATE TYPE actor_type         AS ENUM ('user', 'ai', 'system', 'worker');
CREATE TYPE rsge_record_kind   AS ENUM ('tax_invoice', 'waybill');
CREATE TYPE declaration_type   AS ENUM ('vat', 'income_tax_withholding', 'profit_tax', 'small_business');
CREATE TYPE declaration_status AS ENUM (
    'planned',             -- კალენდარმა შექმნა
    'collecting_data',     -- სინქრონიზაცია (ანგარიშ-ფაქტურები, ზედნადებები, ბანკი)
    'needs_review',        -- AI/წესები ვერ შეთანხმდნენ ან აუხსნელი მონაცემია -> ბუღალტერი
    'not_required',
    'draft_prepared',      -- rs.ge-ზე დრაფტი შენახულია, screenshot + fingerprint გვაქვს
    'awaiting_approval',   -- მომხმარებელს ვთხოვეთ დადასტურება
    'approved',            -- მომხმარებელმა დაადასტურა კონკრეტული fingerprint
    'submitting',
    'submitted',           -- rs.ge-მ მიიღო, ქვითრის ნომერი გვაქვს
    'failed',
    'cancelled'
);
CREATE TYPE automation_kind    AS ENUM ('login_check', 'prepare_zero_declaration', 'submit_declaration',
                                        'sync_invoices', 'sync_waybills', 'canary');
CREATE TYPE run_status         AS ENUM ('queued', 'running', 'waiting_for_user', 'succeeded', 'failed', 'cancelled');
CREATE TYPE otp_status         AS ENUM ('pending', 'answered', 'expired', 'cancelled');
CREATE TYPE llm_purpose        AS ENUM ('extract_document', 'classify_transaction', 'declaration_decision',
                                        'owner_report', 'key_validation');
CREATE TYPE reminder_channel   AS ENUM ('email', 'sms', 'telegram', 'push');
CREATE TYPE consent_kind       AS ENUM ('terms_of_service', 'ai_processing', 'rsge_automation_mandate',
                                        'llm_fallback_other_provider');

-- ------------------------------------------------------------------ tenants & users

CREATE TABLE tenants (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    -- მაგ. {"allow_llm_fallback": false, "auto_submit_zero": false, "zero_activity_policy": {...}}
    settings    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email           text NOT NULL,
    full_name       text,
    phone           text,
    auth_subject    text UNIQUE,          -- OIDC provider-ის sub (Keycloak/Auth0/...)
    mfa_enabled     boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_uq ON users (lower(email));

CREATE TABLE tenant_members (
    tenant_id   uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        member_role NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, user_id)
);

CREATE TABLE consents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    user_id         uuid NOT NULL REFERENCES users(id),
    kind            consent_kind NOT NULL,
    document_version text NOT NULL,       -- რომელ ტექსტს დაეთანხმა
    granted_at      timestamptz NOT NULL DEFAULT now(),
    revoked_at      timestamptz,
    ip              inet
);
CREATE INDEX consents_active_idx ON consents (tenant_id, kind) WHERE revoked_at IS NULL;

-- ------------------------------------------------------------------ taxpayers (კომპანიები)

CREATE TABLE taxpayers (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tin             varchar(11) NOT NULL CHECK (tin ~ '^([0-9]{9}|[0-9]{11})$'),
    legal_name      text NOT NULL,
    legal_form      legal_form NOT NULL,
    regimes         tax_regime[] NOT NULL DEFAULT '{}',
    vat_registered_on date,
    has_employees   boolean NOT NULL DEFAULT false,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, tin),
    UNIQUE (tenant_id, id)
);

-- ------------------------------------------------------------------ secrets

CREATE TABLE llm_credentials (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider        llm_provider NOT NULL,
    label           text,
    ciphertext      bytea NOT NULL,       -- AES-256-GCM(api_key), AAD = tenant|purpose|id
    wrapped_dek     bytea NOT NULL,
    kek_id          text NOT NULL,
    key_hint        text NOT NULL,        -- "…Xy9Z" - UI-სთვის
    model           text NOT NULL,        -- მომხმარებლის არჩეული მოდელი
    is_default      boolean NOT NULL DEFAULT false,
    status          credential_status NOT NULL DEFAULT 'active',
    last_validated_at timestamptz,
    last_error      text,
    created_by      uuid REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    rotated_at      timestamptz,
    revoked_at      timestamptz,
    UNIQUE (tenant_id, id),
    -- გაუქმებული გასაღები "კრიპტო-წაშლილია" (შიფრტექსტი ცარიელია) და default ვეღარ იქნება
    CHECK (status <> 'revoked' OR (NOT is_default AND octet_length(ciphertext) = 0))
);
CREATE UNIQUE INDEX llm_credentials_one_default ON llm_credentials (tenant_id) WHERE is_default;

CREATE TABLE rsge_credentials (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    taxpayer_id     uuid NOT NULL,
    kind            rsge_credential_kind NOT NULL,
    username_hint   text NOT NULL,        -- ნაწილობრივ დაფარული
    secret_ciphertext bytea NOT NULL,     -- {"username","password"} დაშიფრული
    secret_wrapped_dek bytea NOT NULL,
    secret_kek_id   text NOT NULL,
    session_ciphertext bytea,             -- Playwright storage_state (cookies) დაშიფრული
    session_wrapped_dek bytea,
    session_kek_id  text,
    session_expires_at timestamptz,
    otp_phone_hint  text,
    status          credential_status NOT NULL DEFAULT 'active',
    last_login_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, taxpayer_id) REFERENCES taxpayers (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (taxpayer_id, kind),
    CHECK ((session_ciphertext IS NULL) = (session_wrapped_dek IS NULL))
);

-- ------------------------------------------------------------------ documents

CREATE TABLE documents (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    taxpayer_id     uuid NOT NULL,
    source          document_source NOT NULL,
    storage_key     text NOT NULL,        -- S3 (SSE-KMS) ობიექტის გასაღები
    sha256          bytea NOT NULL,
    mime_type       text NOT NULL,
    original_filename text,
    size_bytes      integer NOT NULL CHECK (size_bytes > 0),
    status          document_status NOT NULL DEFAULT 'uploaded',
    uploaded_by     uuid REFERENCES users(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, taxpayer_id) REFERENCES taxpayers (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (taxpayer_id, sha256),         -- ერთი და იგივე ფაილი ორჯერ არ დამუშავდება
    UNIQUE (tenant_id, id)
);
CREATE INDEX documents_review_idx ON documents (tenant_id, status) WHERE status = 'needs_review';

CREATE TABLE llm_calls (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    llm_credential_id uuid,
    provider        llm_provider NOT NULL,
    model           text NOT NULL,
    purpose         llm_purpose NOT NULL,
    input_sha256    bytea,                -- შემავალი ფაილის ჰეში (შიგთავსს არ ვინახავთ)
    output          jsonb,                -- ვალიდირებული structured output
    input_tokens    integer,
    output_tokens   integer,
    attempts        smallint NOT NULL DEFAULT 1,
    latency_ms      integer,
    provider_request_id text,
    error_code      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    -- კომპოზიტური FK: სხვა tenant-ის გასაღებზე მიბმა შეუძლებელია
    FOREIGN KEY (tenant_id, llm_credential_id) REFERENCES llm_credentials (tenant_id, id)
        ON DELETE SET NULL (llm_credential_id)
);
CREATE INDEX llm_calls_usage_idx ON llm_calls (tenant_id, created_at);

CREATE TABLE document_extractions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    document_id     uuid NOT NULL,
    llm_call_id     uuid REFERENCES llm_calls(id),
    extracted       jsonb NOT NULL,       -- ExtractedDocument
    confidence      numeric(4,3) CHECK (confidence BETWEEN 0 AND 1),
    validation_errors   jsonb NOT NULL DEFAULT '[]',
    validation_warnings jsonb NOT NULL DEFAULT '[]',
    is_current      boolean NOT NULL DEFAULT true,
    reviewed_by     uuid REFERENCES users(id),
    reviewed_at     timestamptz,
    corrections     jsonb,                -- ბუღალტრის შესწორებები (few-shot/eval-ისთვის)
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, document_id) REFERENCES documents (tenant_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX document_extractions_current ON document_extractions (document_id) WHERE is_current;

-- ------------------------------------------------------------------ ledger

CREATE TABLE counterparties (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tin             varchar(11) CHECK (tin ~ '^([0-9]{9}|[0-9]{11})$'),
    name            text NOT NULL,
    is_vat_payer    boolean,
    UNIQUE (tenant_id, tin)
);

CREATE TABLE chart_of_accounts (
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code            varchar(10) NOT NULL,
    name_ka         text NOT NULL,
    type            account_type NOT NULL,
    parent_code     varchar(10),
    is_active       boolean NOT NULL DEFAULT true,
    PRIMARY KEY (tenant_id, code),
    FOREIGN KEY (tenant_id, parent_code) REFERENCES chart_of_accounts (tenant_id, code)
);

CREATE TABLE journal_entries (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    taxpayer_id     uuid NOT NULL,
    entry_date      date NOT NULL,
    description     text NOT NULL,
    source_document_id uuid,
    status          entry_status NOT NULL DEFAULT 'draft',
    created_by_type actor_type NOT NULL,
    created_by      uuid REFERENCES users(id),
    ai_confidence   numeric(4,3),
    approved_by     uuid REFERENCES users(id),
    posted_at       timestamptz,
    reverses_entry_id uuid REFERENCES journal_entries(id),
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, taxpayer_id) REFERENCES taxpayers (tenant_id, id),
    FOREIGN KEY (tenant_id, source_document_id) REFERENCES documents (tenant_id, id),
    UNIQUE (tenant_id, id),
    -- AI-ის შექმნილი გატარება დასადასტურებელია ადამიანის მიერ
    CHECK (status = 'draft' OR created_by_type <> 'ai' OR approved_by IS NOT NULL)
);
CREATE INDEX journal_entries_period_idx ON journal_entries (taxpayer_id, entry_date);

CREATE TABLE journal_lines (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       uuid NOT NULL,
    entry_id        uuid NOT NULL,
    account_code    varchar(10) NOT NULL,
    debit           numeric(18,2) NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit          numeric(18,2) NOT NULL DEFAULT 0 CHECK (credit >= 0),
    currency        char(3) NOT NULL DEFAULT 'GEL',
    amount_original numeric(18,2),
    fx_rate         numeric(18,6),        -- ეროვნული ბანკის კურსი ოპერაციის დღეს
    counterparty_id uuid REFERENCES counterparties(id),
    memo            text,
    FOREIGN KEY (tenant_id, entry_id) REFERENCES journal_entries (tenant_id, id) ON DELETE CASCADE,
    FOREIGN KEY (tenant_id, account_code) REFERENCES chart_of_accounts (tenant_id, code),
    CHECK ((debit = 0) <> (credit = 0))   -- ზუსტად ერთი მხარე
);
CREATE INDEX journal_lines_entry_idx ON journal_lines (entry_id);

-- გატარების ბალანსი (დებეტი = კრედიტი) მოწმდება ტრანზაქციის ბოლოს (DEFERRABLE)
CREATE OR REPLACE FUNCTION check_entry_balanced() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_entry uuid;
    v_status entry_status;
    v_debit numeric; v_credit numeric;
BEGIN
    IF TG_TABLE_NAME = 'journal_entries' THEN
        v_entry := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        v_entry := OLD.entry_id;
    ELSE
        v_entry := NEW.entry_id;
    END IF;
    SELECT status INTO v_status FROM journal_entries WHERE id = v_entry;
    IF v_status IS DISTINCT FROM 'draft' AND v_status IS NOT NULL THEN
        SELECT coalesce(sum(debit), 0), coalesce(sum(credit), 0) INTO v_debit, v_credit
        FROM journal_lines WHERE entry_id = v_entry;
        IF v_debit <> v_credit OR v_debit = 0 THEN
            RAISE EXCEPTION 'journal entry % is unbalanced (debit %, credit %)', v_entry, v_debit, v_credit
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NULL;
END $$;

CREATE CONSTRAINT TRIGGER journal_lines_balanced
    AFTER INSERT OR UPDATE OR DELETE ON journal_lines
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_entry_balanced();
CREATE CONSTRAINT TRIGGER journal_entries_balanced
    AFTER INSERT OR UPDATE OF status ON journal_entries
    DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_entry_balanced();

-- გატარებულ ჩანაწერს არ ვცვლით - შესწორება მხოლოდ შემობრუნებით (reversal)
CREATE OR REPLACE FUNCTION forbid_posted_line_changes() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_status entry_status;
BEGIN
    SELECT status INTO v_status FROM journal_entries WHERE id = COALESCE(NEW.entry_id, OLD.entry_id);
    IF v_status IS NOT NULL AND v_status <> 'draft' THEN
        RAISE EXCEPTION 'lines of a posted journal entry are immutable; create a reversal instead'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN COALESCE(NEW, OLD);
END $$;
CREATE TRIGGER journal_lines_immutable BEFORE UPDATE OR DELETE ON journal_lines
    FOR EACH ROW EXECUTE FUNCTION forbid_posted_line_changes();

-- ------------------------------------------------------------------ bank & rs.ge data

CREATE TABLE bank_accounts (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    taxpayer_id     uuid NOT NULL,
    iban            varchar(22) NOT NULL CHECK (iban ~ '^GE[0-9]{2}[A-Z]{2}[0-9]{16}$'),
    currency        char(3) NOT NULL,
    ledger_account_code varchar(10) NOT NULL,
    FOREIGN KEY (tenant_id, taxpayer_id) REFERENCES taxpayers (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (tenant_id, iban, currency),
    UNIQUE (tenant_id, id)
);

CREATE TABLE bank_transactions (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    bank_account_id uuid NOT NULL,
    external_id     text NOT NULL,        -- ბანკის ოპერაციის ID (idempotent იმპორტი)
    booked_on       date NOT NULL,
    amount          numeric(18,2) NOT NULL, -- + შემოსავალი, - გასავალი
    currency        char(3) NOT NULL,
    counterparty_name text,
    counterparty_iban text,
    counterparty_tin varchar(11),
    purpose         text,
    classification  jsonb,                -- AI: {"category","account_code","confidence","is_taxable_revenue"}
    journal_entry_id uuid REFERENCES journal_entries(id),
    source_document_id uuid,
    FOREIGN KEY (tenant_id, bank_account_id) REFERENCES bank_accounts (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (bank_account_id, external_id)
);
CREATE INDEX bank_transactions_unmatched_idx ON bank_transactions (tenant_id, booked_on) WHERE journal_entry_id IS NULL;

CREATE TABLE rsge_records (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    taxpayer_id     uuid NOT NULL,
    kind            rsge_record_kind NOT NULL,
    rs_id           bigint NOT NULL,      -- rs.ge-ს შიდა ID
    direction       text NOT NULL CHECK (direction IN ('incoming', 'outgoing')),
    number          text,
    status          text,
    seller_tin      varchar(11),
    buyer_tin       varchar(11),
    operation_date  date,
    net_amount      numeric(18,2),
    vat_amount      numeric(18,2),
    total_amount    numeric(18,2),
    raw             jsonb NOT NULL,
    fetched_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, taxpayer_id) REFERENCES taxpayers (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (taxpayer_id, kind, rs_id)
);
CREATE INDEX rsge_records_period_idx ON rsge_records (taxpayer_id, kind, operation_date);

-- ------------------------------------------------------------------ declarations

CREATE TABLE declarations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    taxpayer_id     uuid NOT NULL,
    type            declaration_type NOT NULL,
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    due_date        date NOT NULL,
    status          declaration_status NOT NULL DEFAULT 'planned',
    is_zero         boolean,
    facts           jsonb,                -- PeriodFacts (დეტერმინისტული)
    rule_decision   jsonb,
    ai_decision     jsonb,
    ai_llm_call_id  uuid REFERENCES llm_calls(id),
    form_values     jsonb,                -- rs.ge ფორმიდან წაკითხული მნიშვნელობები
    form_fingerprint text,                -- sha256 (prepare ფაზა)
    draft_screenshot_key text,
    approved_by     uuid REFERENCES users(id),
    approved_at     timestamptz,
    approved_fingerprint text,
    rs_receipt_number text,
    receipt_screenshot_key text,
    submitted_at    timestamptz,
    last_error_code text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, taxpayer_id) REFERENCES taxpayers (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (tenant_id, id),
    CHECK (period_end >= period_start),
    -- ადამიანის დადასტურების გარეშე წარდგენა DB-ის დონეზეც შეუძლებელია
    CHECK (status NOT IN ('approved', 'submitting', 'submitted')
           OR (approved_by IS NOT NULL AND approved_at IS NOT NULL
               AND approved_fingerprint IS NOT NULL AND approved_fingerprint = form_fingerprint)),
    CHECK (status <> 'submitted' OR (rs_receipt_number IS NOT NULL AND submitted_at IS NOT NULL))
);
CREATE UNIQUE INDEX declarations_one_active_per_period
    ON declarations (taxpayer_id, type, period_start) WHERE status <> 'cancelled';
CREATE INDEX declarations_due_idx ON declarations (due_date) WHERE status NOT IN ('submitted', 'not_required', 'cancelled');

CREATE TABLE declaration_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       uuid NOT NULL,
    declaration_id  uuid NOT NULL,
    from_status     declaration_status,
    to_status       declaration_status NOT NULL,
    actor_type      actor_type NOT NULL,
    actor_id        uuid,
    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, declaration_id) REFERENCES declarations (tenant_id, id) ON DELETE CASCADE
);
CREATE INDEX declaration_events_decl_idx ON declaration_events (declaration_id, id);

-- სტატუსების მანქანის უსაფრთხოების ინვარიანტები (DB-ის დონეზე, worker-ის ბაგის მიუხედავად):
-- * approved     - მხოლოდ draft_prepared / awaiting_approval-დან (ადამიანმა ნახა ფორმა);
-- * submitting   - მხოლოდ approved-დან (ან failed-დან ხელახლა, თუ დადასტურება ისევ ვალიდურია - CHECK);
-- * submitted    - მხოლოდ submitting-დან (crash-ის შემდეგ worker-მა იცის, რომ rs.ge ჯერ უნდა შეამოწმოს);
-- * submitted/cancelled - საბოლოოა. დაზუსტებული დეკლარაცია = ახალი ჩანაწერი.
CREATE OR REPLACE FUNCTION check_declaration_transition() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status IN ('approved', 'submitting', 'submitted') THEN
            RAISE EXCEPTION 'declaration cannot be created in status %', NEW.status USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.status = 'submitted' AND ROW(NEW.status, NEW.type, NEW.taxpayer_id, NEW.period_start, NEW.period_end,
            NEW.is_zero, NEW.form_values, NEW.form_fingerprint, NEW.approved_by, NEW.approved_at,
            NEW.approved_fingerprint, NEW.rs_receipt_number, NEW.submitted_at)
        IS DISTINCT FROM ROW(OLD.status, OLD.type, OLD.taxpayer_id, OLD.period_start, OLD.period_end,
            OLD.is_zero, OLD.form_values, OLD.form_fingerprint, OLD.approved_by, OLD.approved_at,
            OLD.approved_fingerprint, OLD.rs_receipt_number, OLD.submitted_at) THEN
        RAISE EXCEPTION 'submitted declaration % is immutable; file an amended declaration instead', OLD.id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.status = 'cancelled'
           OR (NEW.status = 'approved' AND OLD.status NOT IN ('draft_prepared', 'awaiting_approval'))
           OR (NEW.status = 'submitting' AND OLD.status NOT IN ('approved', 'failed'))
           OR (NEW.status = 'submitted' AND OLD.status <> 'submitting') THEN
            RAISE EXCEPTION 'invalid declaration transition % -> %', OLD.status, NEW.status
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER declarations_transition BEFORE INSERT OR UPDATE ON declarations
    FOR EACH ROW EXECUTE FUNCTION check_declaration_transition();

CREATE OR REPLACE FUNCTION log_declaration_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.status IS DISTINCT FROM OLD.status THEN
        INSERT INTO declaration_events (tenant_id, declaration_id, from_status, to_status, actor_type, actor_id, note)
        VALUES (NEW.tenant_id, NEW.id, CASE WHEN TG_OP = 'UPDATE' THEN OLD.status END, NEW.status,
                coalesce(nullif(current_setting('app.actor_type', true), ''), 'system')::actor_type,
                nullif(current_setting('app.actor_id', true), '')::uuid,
                nullif(current_setting('app.note', true), ''));
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER declarations_status_log AFTER INSERT OR UPDATE OF status ON declarations
    FOR EACH ROW EXECUTE FUNCTION log_declaration_status();

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER declarations_touch BEFORE UPDATE ON declarations
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- ------------------------------------------------------------------ automation

CREATE TABLE automation_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    taxpayer_id     uuid NOT NULL,
    kind            automation_kind NOT NULL,
    declaration_id  uuid REFERENCES declarations(id),
    status          run_status NOT NULL DEFAULT 'queued',
    attempt         smallint NOT NULL DEFAULT 1,
    started_at      timestamptz,
    finished_at     timestamptz,
    error_code      text,                 -- RsGeError.code
    error_message   text,
    evidence_prefix text,                 -- S3: screenshots/, trace.zip, manifest.json
    created_at      timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id, taxpayer_id) REFERENCES taxpayers (tenant_id, id) ON DELETE CASCADE
);
-- ერთ გადამხდელზე ერთდროულად მხოლოდ ერთი ბრაუზერის სესია
CREATE UNIQUE INDEX automation_runs_one_active ON automation_runs (taxpayer_id)
    WHERE status IN ('running', 'waiting_for_user');

CREATE TABLE otp_challenges (
    id              text PRIMARY KEY,     -- OtpChallenge.id; კოდი აქ არ ინახება (მხოლოდ Redis-ში, წუთებით)
    tenant_id       uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    automation_run_id uuid NOT NULL REFERENCES automation_runs(id) ON DELETE CASCADE,
    phone_hint      text,
    status          otp_status NOT NULL DEFAULT 'pending',
    created_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    answered_by     uuid REFERENCES users(id),
    answered_at     timestamptz
);

-- ------------------------------------------------------------------ calendar & reminders

CREATE TABLE tax_calendar_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    taxpayer_id     uuid NOT NULL,
    rule_code       text NOT NULL,        -- tax_calendar.DeadlineRule.code
    period_label    text NOT NULL,        -- '2026-09' | '2026'
    statutory_date  date NOT NULL,
    due_date        date NOT NULL,
    declaration_id  uuid REFERENCES declarations(id),
    done_at         timestamptz,
    FOREIGN KEY (tenant_id, taxpayer_id) REFERENCES taxpayers (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (taxpayer_id, rule_code, period_label),
    UNIQUE (tenant_id, id)
);

CREATE TABLE reminders (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL,
    event_id        uuid NOT NULL,
    user_id         uuid NOT NULL REFERENCES users(id),
    channel         reminder_channel NOT NULL,
    send_at         timestamptz NOT NULL,
    sent_at         timestamptz,
    FOREIGN KEY (tenant_id, event_id) REFERENCES tax_calendar_events (tenant_id, id) ON DELETE CASCADE,
    UNIQUE (event_id, user_id, channel, send_at)
);
CREATE INDEX reminders_due_idx ON reminders (send_at) WHERE sent_at IS NULL;

-- ------------------------------------------------------------------ audit log (append-only, hash chain)

CREATE TABLE audit_log (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenants(id),
    seq             bigint NOT NULL,      -- tenant-ის ჯაჭვში რიგითი ნომერი (1, 2, 3, ... უწყვეტი)
    actor_type      actor_type NOT NULL,
    actor_id        uuid,
    action          text NOT NULL,        -- 'llm_key.created', 'declaration.approved', 'rsge.submitted', ...
    entity_type     text,
    entity_id       text,
    details         jsonb NOT NULL DEFAULT '{}',
    ip              inet,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    prev_hash       bytea,
    hash            bytea NOT NULL,
    UNIQUE (tenant_id, seq)               -- ჯაჭვის "განშტოება" შეუძლებელია (კონკურენტული ჩაწერისასაც)
);

-- ჰეშის შემავალი მონაცემები - jsonb მასივი: ცალსახაა (NULL != ცარიელი სტრიქონი, ველები არ "იძვრის")
-- და არ არის დამოკიდებული სესიის TimeZone/DateStyle პარამეტრებზე (created_at - UTC, ფიქსირებული ფორმატი).
CREATE OR REPLACE FUNCTION audit_log_hash(p_prev bytea, p_tenant uuid, p_seq bigint, p_actor_type actor_type,
    p_actor_id uuid, p_action text, p_entity_type text, p_entity_id text, p_details jsonb, p_ip inet,
    p_created_at timestamptz) RETURNS bytea
LANGUAGE sql STABLE AS $$
    SELECT sha256(convert_to(jsonb_build_array(
        encode(coalesce(p_prev, ''::bytea), 'hex'), p_tenant::text, p_seq, p_actor_type::text, p_actor_id::text,
        p_action, p_entity_type, p_entity_id, p_details, p_ip::text,
        to_char(p_created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'))::text, 'UTF8'))
$$;

CREATE OR REPLACE FUNCTION audit_log_chain() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_last record;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('audit:' || NEW.tenant_id::text, 0));
    SELECT seq, hash INTO v_last FROM audit_log WHERE tenant_id = NEW.tenant_id ORDER BY seq DESC LIMIT 1;
    NEW.seq := coalesce(v_last.seq, 0) + 1;
    NEW.prev_hash := v_last.hash;
    NEW.hash := audit_log_hash(NEW.prev_hash, NEW.tenant_id, NEW.seq, NEW.actor_type, NEW.actor_id, NEW.action,
                               NEW.entity_type, NEW.entity_id, NEW.details, NEW.ip, NEW.created_at);
    RETURN NEW;
END $$;
CREATE TRIGGER audit_log_chain BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION audit_log_chain();

-- მიმდინარე tenant-ის ჯაჭვის შემოწმება: ჰეში ხელახლა ითვლება, prev_hash და seq-ის უწყვეტობა მოწმდება.
-- first_broken_seq = NULL -> ჯაჭვი მთელია. (ბოლო ჩანაწერების წაშლის აღმოსაჩენად ბოლო ჰეში პერიოდულად
-- გარე, WORM საცავშიც უნდა ჩაიწეროს - "anchoring".)
CREATE OR REPLACE FUNCTION audit_log_verify(OUT checked bigint, OUT first_broken_seq bigint, OUT last_hash bytea)
LANGUAGE plpgsql STABLE AS $$
DECLARE r record;
BEGIN
    checked := 0;
    FOR r IN SELECT * FROM audit_log WHERE tenant_id = app_current_tenant() ORDER BY seq LOOP
        IF r.seq <> checked + 1 OR r.prev_hash IS DISTINCT FROM last_hash
           OR r.hash IS DISTINCT FROM audit_log_hash(r.prev_hash, r.tenant_id, r.seq, r.actor_type, r.actor_id,
                   r.action, r.entity_type, r.entity_id, r.details, r.ip, r.created_at) THEN
            first_broken_seq := r.seq;
            RETURN;
        END IF;
        checked := checked + 1;
        last_hash := r.hash;
    END LOOP;
END $$;

CREATE OR REPLACE FUNCTION forbid_modification() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION forbid_modification();
CREATE TRIGGER declaration_events_append_only BEFORE UPDATE OR DELETE ON declaration_events
    FOR EACH ROW EXECUTE FUNCTION forbid_modification();

-- ------------------------------------------------------------------ row level security

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tenant_members', 'consents', 'taxpayers', 'llm_credentials', 'rsge_credentials', 'documents',
        'llm_calls', 'document_extractions', 'counterparties', 'chart_of_accounts', 'journal_entries',
        'journal_lines', 'bank_accounts', 'bank_transactions', 'rsge_records', 'declarations',
        'declaration_events', 'automation_runs', 'otp_challenges', 'tax_calendar_events', 'reminders',
        'audit_log'
    ] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I USING (tenant_id = app_current_tenant()) '
            'WITH CHECK (tenant_id = app_current_tenant())', t);
    END LOOP;
END $$;

-- users გლობალურია (ერთი ადამიანი შეიძლება რამდენიმე tenant-ის წევრი იყოს): აპლიკაცია ხედავს მხოლოდ
-- მიმდინარე tenant-ის წევრებს. რეგისტრაცია/პროფილის ცვლილება - ცალკე, პრივილეგირებული auth სერვისით.
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY users_of_current_tenant ON users FOR SELECT USING (
    EXISTS (SELECT 1 FROM tenant_members m WHERE m.user_id = users.id AND m.tenant_id = app_current_tenant()));

ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_self ON tenants USING (id = app_current_tenant()) WITH CHECK (id = app_current_tenant());

-- ------------------------------------------------------------------ grants

GRANT USAGE ON SCHEMA public TO ai_accountant_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO ai_accountant_app;
REVOKE UPDATE, DELETE ON audit_log, declaration_events FROM ai_accountant_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO ai_accountant_app;

