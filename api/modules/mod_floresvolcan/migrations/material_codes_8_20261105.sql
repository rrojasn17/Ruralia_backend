-- Recorte solicitado del catálogo; no modifica IDs ni snapshots históricos.
SET LOCAL lock_timeout='5s';
LOCK TABLE fv_sap_products IN SHARE ROW EXCLUSIVE MODE;
DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM fv_sap_products GROUP BY upper(left(code,8)) HAVING count(*)>1) THEN
  RAISE EXCEPTION 'El recorte produce códigos duplicados';
 END IF;
 INSERT INTO fv_module_settings(key,value_text,is_secret,updated_at)
 SELECT 'maintenance.material_codes_8.20261105',
  json_build_object('reason','Usuario solicita códigos de materiales de máximo ocho caracteres','changes',
    (SELECT json_agg(json_build_object('id',id,'old_code',code,'new_code',left(code,8))) FROM fv_sap_products WHERE length(code)>8))::text,
  false,CURRENT_TIMESTAMP
 ON CONFLICT(key) DO NOTHING;
 UPDATE fv_sap_products SET code=left(code,8) WHERE length(code)>8;
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fv_sap_code_eight_characters' AND conrelid='fv_sap_products'::regclass) THEN
  ALTER TABLE fv_sap_products ADD CONSTRAINT fv_sap_code_eight_characters CHECK (length(code) BETWEEN 1 AND 8);
 END IF;
END $$;
