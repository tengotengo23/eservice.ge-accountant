-- 0003: tenant-ის ცხრილებს შორის ყველა FK კომპოზიტურია - (tenant_id, x_id).
--
-- PostgreSQL FK-ს RLS-ის გარეშე ამოწმებს. მარტო x_id-ზე FK-ით tenant A-ს შეეძლო თავისი ჩანაწერის მიბმა
-- tenant B-ის ჩანაწერზე (მაგ. automation_runs.declaration_id) და FK-ის შეცდომით სხვისი id-ის არსებობის
-- გამოცნობა. კომპოზიტური FK მშობელს მხოლოდ იმავე tenant-ში ეძებს.
-- თუ არსებული მონაცემი ამ წესს არღვევს, მიგრაცია ჩერდება - ასეთი სტრიქონი ინციდენტია და ხელით უნდა გაირკვეს.

ALTER TABLE llm_calls       ADD CONSTRAINT llm_calls_tenant_id_uq       UNIQUE (tenant_id, id);
ALTER TABLE counterparties  ADD CONSTRAINT counterparties_tenant_id_uq  UNIQUE (tenant_id, id);
ALTER TABLE automation_runs ADD CONSTRAINT automation_runs_tenant_id_uq UNIQUE (tenant_id, id);

ALTER TABLE document_extractions
    DROP CONSTRAINT document_extractions_llm_call_id_fkey,
    ADD CONSTRAINT document_extractions_llm_call_id_fkey
        FOREIGN KEY (tenant_id, llm_call_id) REFERENCES llm_calls (tenant_id, id);

ALTER TABLE journal_entries
    DROP CONSTRAINT journal_entries_reverses_entry_id_fkey,
    ADD CONSTRAINT journal_entries_reverses_entry_id_fkey
        FOREIGN KEY (tenant_id, reverses_entry_id) REFERENCES journal_entries (tenant_id, id);

ALTER TABLE journal_lines
    DROP CONSTRAINT journal_lines_counterparty_id_fkey,
    ADD CONSTRAINT journal_lines_counterparty_id_fkey
        FOREIGN KEY (tenant_id, counterparty_id) REFERENCES counterparties (tenant_id, id);

ALTER TABLE bank_transactions
    DROP CONSTRAINT bank_transactions_journal_entry_id_fkey,
    ADD CONSTRAINT bank_transactions_journal_entry_id_fkey
        FOREIGN KEY (tenant_id, journal_entry_id) REFERENCES journal_entries (tenant_id, id),
    -- ადრე FK საერთოდ არ ჰქონდა
    ADD CONSTRAINT bank_transactions_source_document_id_fkey
        FOREIGN KEY (tenant_id, source_document_id) REFERENCES documents (tenant_id, id);

ALTER TABLE declarations
    DROP CONSTRAINT declarations_ai_llm_call_id_fkey,
    ADD CONSTRAINT declarations_ai_llm_call_id_fkey
        FOREIGN KEY (tenant_id, ai_llm_call_id) REFERENCES llm_calls (tenant_id, id);

ALTER TABLE automation_runs
    DROP CONSTRAINT automation_runs_declaration_id_fkey,
    ADD CONSTRAINT automation_runs_declaration_id_fkey
        FOREIGN KEY (tenant_id, declaration_id) REFERENCES declarations (tenant_id, id);

ALTER TABLE otp_challenges
    DROP CONSTRAINT otp_challenges_automation_run_id_fkey,
    ADD CONSTRAINT otp_challenges_automation_run_id_fkey
        FOREIGN KEY (tenant_id, automation_run_id) REFERENCES automation_runs (tenant_id, id) ON DELETE CASCADE;

ALTER TABLE tax_calendar_events
    DROP CONSTRAINT tax_calendar_events_declaration_id_fkey,
    ADD CONSTRAINT tax_calendar_events_declaration_id_fkey
        FOREIGN KEY (tenant_id, declaration_id) REFERENCES declarations (tenant_id, id);
