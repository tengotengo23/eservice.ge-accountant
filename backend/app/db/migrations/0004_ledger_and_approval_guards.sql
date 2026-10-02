-- 0004: გატარებული ჟურნალის უცვლელობა და დადასტურების ავტორის შემოწმება DB-ის დონეზე.

-- ------------------------------------------------------------------ ledger
-- გატარებულ ჩანაწერს სტრიქონს ვერც დავამატებთ: დაბალანსებული წყვილი (დ X / კ X) ბალანსს არ არღვევს,
-- მაგრამ გატარებულ ოპერაციას შეცვლიდა. ნაკადი: draft -> სტრიქონები -> UPDATE status = 'posted'.
DROP TRIGGER journal_lines_immutable ON journal_lines;
CREATE TRIGGER journal_lines_immutable BEFORE INSERT OR UPDATE OR DELETE ON journal_lines
    FOR EACH ROW EXECUTE FUNCTION forbid_posted_line_changes();

-- სათაურიც (თარიღი, აღწერა, დოკუმენტი): თარიღის შეცვლა ოპერაციას სხვა საგადასახადო პერიოდში "გადაიტანდა".
-- გატარებულზე ერთადერთი დაშვებული ცვლილება: posted -> reversed (შესწორება - შემობრუნებითი გატარებით).
CREATE FUNCTION forbid_posted_entry_changes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'draft' THEN
        RETURN COALESCE(NEW, OLD);
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.status = 'posted' AND NEW.status = 'reversed'
       AND to_jsonb(NEW) - 'status' = to_jsonb(OLD) - 'status' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'journal entry % is %: posted entries are immutable; create a reversal instead', OLD.id, OLD.status
        USING ERRCODE = 'integrity_constraint_violation';
END $$;
CREATE TRIGGER journal_entries_immutable BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION forbid_posted_entry_changes();

-- ------------------------------------------------------------------ declaration approval
-- approved_* ველებს წერს მხოლოდ თავად დამდასტურებელი (app.actor_type = 'user', approved_by = app.actor_id),
-- რომელიც ამ ორგანიზაციის owner/accountant-ია; ან 'system' - მხოლოდ ნულოვანზე და tenant-ის auto_submit_zero
-- თანხმობით (docs 4.3). worker-ი (ან მისი ბაგი) და viewer დადასტურებას ვერ "დაწერს". app.actor_* აპლიკაცია
-- აყენებს, ამიტომ ეს დაცვაა შეცდომებისგან; გატეხილი აპლიკაციისგან - ცალკე DB როლი (იხ. docs).
-- ტრიგერის სახელი declarations_transition-ის შემდეგ არის ანბანში, რათა ჯერ სტატუსის წესი შემოწმდეს.
CREATE FUNCTION verify_declaration_approver() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_actor_type text := nullif(current_setting('app.actor_type', true), '');
    v_actor_id   uuid := nullif(current_setting('app.actor_id', true), '')::uuid;
BEGIN
    IF NEW.approved_by IS NULL OR (TG_OP = 'UPDATE'
       AND (NEW.approved_by, NEW.approved_at, NEW.approved_fingerprint)
           IS NOT DISTINCT FROM (OLD.approved_by, OLD.approved_at, OLD.approved_fingerprint)) THEN
        RETURN NEW;
    END IF;
    IF v_actor_type = 'user' THEN
        IF NEW.approved_by IS DISTINCT FROM v_actor_id THEN
            RAISE EXCEPTION 'approval must be recorded by the approving user' USING ERRCODE = 'insufficient_privilege';
        END IF;
    ELSIF v_actor_type = 'system' THEN
        IF NOT coalesce(NEW.is_zero, false) OR NOT coalesce(
               (SELECT (t.settings ->> 'auto_submit_zero')::boolean FROM tenants t WHERE t.id = NEW.tenant_id), false) THEN
            RAISE EXCEPTION 'automatic approval needs a zero declaration and the auto_submit_zero consent'
                USING ERRCODE = 'insufficient_privilege';
        END IF;
    ELSE
        RAISE EXCEPTION 'a declaration can be approved only by a user (actor_type=%)', coalesce(v_actor_type, 'unset')
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM tenant_members m WHERE m.tenant_id = NEW.tenant_id
                   AND m.user_id = NEW.approved_by AND m.role IN ('owner', 'accountant')) THEN
        RAISE EXCEPTION 'approver must be an owner or accountant of this organization'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER declarations_verify_approver BEFORE INSERT OR UPDATE ON declarations
    FOR EACH ROW EXECUTE FUNCTION verify_declaration_approver();
