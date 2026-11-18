-- Limpieza solicitada expresamente: quitar el historial importado de las camas.
-- No borrar inventario, catálogos, trabajadores ni órdenes operativas nuevas.
SET LOCAL lock_timeout = '5s';
LOCK TABLE fv_production_batches, fv_planting_records, fv_harvest_records, fv_bed_cycles IN SHARE ROW EXCLUSIVE MODE;
CREATE TEMP TABLE fv_cleanup_batches ON COMMIT DROP AS
SELECT b.id,b.bed_cycle_id FROM fv_production_batches b
JOIN fv_planting_orders o ON o.id=b.planting_order_id
WHERE o.status='historical' AND b.status='historical';
CREATE TEMP TABLE fv_cleanup_cycles ON COMMIT DROP AS
SELECT DISTINCT t.bed_cycle_id AS id FROM fv_cleanup_batches t
WHERE t.bed_cycle_id IS NOT NULL AND NOT EXISTS
(SELECT 1 FROM fv_production_batches b WHERE b.bed_cycle_id=t.bed_cycle_id AND b.id NOT IN (SELECT id FROM fv_cleanup_batches));
CREATE TEMP TABLE fv_cleanup_segments ON COMMIT DROP AS
SELECT id FROM fv_planting_records WHERE production_batch_id IN (SELECT id FROM fv_cleanup_batches);
CREATE TEMP TABLE fv_cleanup_harvest_orders ON COMMIT DROP AS
SELECT id FROM fv_harvest_orders WHERE production_batch_id IN (SELECT id FROM fv_cleanup_batches);
-- No usar CASCADE: referencias inesperadas abortan y revierten la transacción.
DELETE FROM fv_paper_captures WHERE harvest_order_id IN (SELECT id FROM fv_cleanup_harvest_orders)
 OR (operation='planting' AND linked_record_id IN (SELECT id FROM fv_cleanup_segments));
DELETE FROM fv_historical_overage_approvals WHERE production_batch_id IN (SELECT id FROM fv_cleanup_batches);
DELETE FROM fv_technical_closures WHERE production_batch_id IN (SELECT id FROM fv_cleanup_batches);
DELETE FROM fv_segment_reconciliations WHERE planting_record_id IN (SELECT id FROM fv_cleanup_segments);
DELETE FROM fv_harvest_records WHERE production_batch_id IN (SELECT id FROM fv_cleanup_batches);
DELETE FROM fv_harvest_orders WHERE id IN (SELECT id FROM fv_cleanup_harvest_orders);
DELETE FROM fv_planting_records WHERE id IN (SELECT id FROM fv_cleanup_segments);
DELETE FROM fv_production_batches WHERE id IN (SELECT id FROM fv_cleanup_batches);
DELETE FROM fv_bed_cycles WHERE id IN (SELECT id FROM fv_cleanup_cycles);
INSERT INTO fv_module_settings(key,value_text,is_secret,updated_at)
VALUES('maintenance.remove_imported_bed_history.20261105',
 json_build_object('reason','Usuario solicita eliminar históricos de las camas; respaldo previo conservado',
 'deleted_batches',(SELECT count(*) FROM fv_cleanup_batches),'deleted_segments',(SELECT count(*) FROM fv_cleanup_segments),
 'deleted_cycles',(SELECT count(*) FROM fv_cleanup_cycles))::text,false,CURRENT_TIMESTAMP)
ON CONFLICT(key) DO NOTHING;
