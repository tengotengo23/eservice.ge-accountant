-- 0002: worker-ის ნაკადი (დეკლარაციის მომზადება/წარდგენა), rs.ge მონაცემების სასიცოცხლო ციკლი, scheduler.

-- ------------------------------------------------------------------ rs.ge credentials
ALTER TABLE rsge_credentials
    ADD COLUMN created_by uuid REFERENCES users(id),
    ADD COLUMN revoked_at timestamptz,
    ADD COLUMN last_error_code text,
    ADD CONSTRAINT rsge_credentials_tenant_id_uq UNIQUE (tenant_id, id),
    -- გაუქმება = კრიპტო-წაშლა: პაროლი და სესია აღარ აღდგება
    ADD CONSTRAINT rsge_credentials_revoked_shredded
        CHECK (status <> 'revoked' OR (octet_length(secret_ciphertext) = 0 AND session_ciphertext IS NULL));

-- ------------------------------------------------------------------ automation
CREATE INDEX automation_runs_declaration_idx ON automation_runs (declaration_id, created_at);
CREATE INDEX otp_challenges_pending_idx ON otp_challenges (tenant_id, expires_at) WHERE status = 'pending';

-- ------------------------------------------------------------------ scheduler
-- ყოველდღიურ დამგეგმავს (planner) ყველა tenant-ის გავლა სჭირდება, აპლიკაციის როლი კი RLS-ით მხოლოდ
-- ერთ tenant-ს ხედავს. ვიწრო, კონტროლირებადი გამონაკლისი: SECURITY DEFINER ფუნქცია აბრუნებს მხოლოდ
-- id-ებს; თითოეულ tenant-ში შემდგომი მუშაობა ისევ ჩვეულებრივ, RLS-ის ქვეშ ხდება.
CREATE FUNCTION system_tenant_ids() RETURNS SETOF uuid
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT id FROM public.tenants ORDER BY id
$$;
REVOKE ALL ON FUNCTION system_tenant_ids() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION system_tenant_ids() TO ai_accountant_app;

-- FORCE RLS მფლობელზეც მოქმედებს: ფუნქციის მფლობელს (მიგრაციის როლს) ყველა tenant-ის წაკითხვა სჭირდება
DO $$ BEGIN
    EXECUTE format('CREATE POLICY tenants_owner_scan ON tenants FOR SELECT TO %I USING (true)', current_user);
END $$;
