-- Compatibilidad con importadores anteriores: siempre persistir cuatro dígitos.
-- Ejecutar en una transacción. No modifica IDs ni relaciones históricas.
SET LOCAL lock_timeout = '5s';
LOCK TABLE fv_workers IN SHARE ROW EXCLUSIVE MODE;
ALTER TABLE fv_workers ADD COLUMN IF NOT EXISTS legacy_employee_code VARCHAR(50);
CREATE OR REPLACE FUNCTION fv_assign_worker_code() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE candidate TEXT;
BEGIN
    IF NEW.employee_code IS NULL OR NEW.employee_code !~ '^[0-9]{4}$' THEN
        NEW.legacy_employee_code := COALESCE(NEW.legacy_employee_code, NEW.employee_code);
        PERFORM pg_advisory_xact_lock(48721, 1104);
        SELECT n::text INTO candidate
        FROM generate_series(1000,9999) AS n
        WHERE NOT EXISTS (SELECT 1 FROM fv_workers w WHERE w.employee_code=n::text)
        ORDER BY random() LIMIT 1;
        IF candidate IS NULL THEN
            RAISE EXCEPTION 'Se agotaron los códigos de cuatro dígitos';
        END IF;
        NEW.employee_code := candidate;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS fv_worker_code_format ON fv_workers;
CREATE TRIGGER fv_worker_code_format BEFORE INSERT OR UPDATE OF employee_code
ON fv_workers FOR EACH ROW EXECUTE FUNCTION fv_assign_worker_code();
UPDATE fv_workers SET employee_code=employee_code
WHERE employee_code IS NULL OR employee_code !~ '^[0-9]{4}$';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fv_worker_code_four_digits' AND conrelid='fv_workers'::regclass) THEN
        ALTER TABLE fv_workers ADD CONSTRAINT fv_worker_code_four_digits CHECK (employee_code ~ '^[0-9]{4}$');
    END IF;
END $$;
