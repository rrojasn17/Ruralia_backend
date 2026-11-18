-- Acción de mantenimiento solicitada por el usuario: archivar Excel y liberar camas.
-- Mantiene intactos los registros de siembra, cosecha, cantidades y vínculos.
SET LOCAL lock_timeout = '5s';
LOCK TABLE fv_bed_cycles, fv_production_batches, fv_planting_orders IN SHARE ROW EXCLUSIVE MODE;
DO $$
DECLARE cycle_ids integer[]; batch_ids integer[]; snapshot jsonb;
BEGIN
    IF EXISTS (SELECT 1 FROM fv_module_settings WHERE key='maintenance.release_imported_beds.20261105') THEN RETURN; END IF;
    SELECT array_agg(c.id) INTO cycle_ids FROM fv_bed_cycles c
    WHERE c.status<>'cleaned'
      AND EXISTS (SELECT 1 FROM fv_production_batches b JOIN fv_planting_orders o ON o.id=b.planting_order_id WHERE b.bed_cycle_id=c.id AND o.status='historical')
      AND NOT EXISTS (SELECT 1 FROM fv_production_batches b JOIN fv_planting_orders o ON o.id=b.planting_order_id WHERE b.bed_cycle_id=c.id AND o.status<>'historical');
    SELECT array_agg(b.id) INTO batch_ids FROM fv_production_batches b JOIN fv_planting_orders o ON o.id=b.planting_order_id
    WHERE b.bed_cycle_id=ANY(cycle_ids) AND o.status='historical';
    snapshot=jsonb_build_object('requested_at',CURRENT_TIMESTAMP,'reason','Usuario solicita liberar las camas importadas, conservar históricos y continuar usando la aplicación; no es una conciliación de cantidades.',
        'cycles_before',(SELECT jsonb_agg(to_jsonb(c)) FROM fv_bed_cycles c WHERE c.id=ANY(cycle_ids)),
        'batches_before',(SELECT jsonb_agg(jsonb_build_object('id',b.id,'status',b.status,'closed_at',b.closed_at)) FROM fv_production_batches b WHERE b.id=ANY(batch_ids)),
        'planting_records_before',(SELECT count(*) FROM fv_planting_records),
        'harvest_records_before',(SELECT count(*) FROM fv_harvest_records));
    INSERT INTO fv_module_settings(key,value_text,is_secret,updated_at)
    VALUES('maintenance.release_imported_beds.20261105',snapshot::text,false,CURRENT_TIMESTAMP);
    UPDATE fv_production_batches SET status='historical' WHERE id=ANY(batch_ids);
    UPDATE fv_bed_cycles SET status='cleaned',cleaned_at=CURRENT_TIMESTAMP,
        cleaning_notes=concat_ws(E'\n',cleaning_notes,'Liberada por solicitud expresa del usuario. Ciclo importado archivado; siembras, cosechas y diferencias históricas conservadas. Referencia: maintenance.release_imported_beds.20261105')
    WHERE id=ANY(cycle_ids);
    IF (SELECT count(*) FROM fv_planting_records)<>(snapshot->>'planting_records_before')::bigint
       OR (SELECT count(*) FROM fv_harvest_records)<>(snapshot->>'harvest_records_before')::bigint THEN
       RAISE EXCEPTION 'Cambió el número de registros históricos';
    END IF;
END $$;
