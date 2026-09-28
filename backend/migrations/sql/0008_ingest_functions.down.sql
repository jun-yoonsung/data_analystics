ALTER TABLE core.competition_stage DROP COLUMN IF EXISTS ingest_run_id;
ALTER TABLE core.season DROP COLUMN IF EXISTS ingest_run_id;
DROP FUNCTION IF EXISTS util.collect_lock_key(text);
REVOKE EXECUTE ON FUNCTION util.ensure_event_partition(integer) FROM app_ingest;
GRANT EXECUTE ON FUNCTION util.ensure_event_partition(integer) TO PUBLIC;
ALTER FUNCTION util.ensure_event_partition(integer) RESET search_path;
ALTER FUNCTION util.ensure_event_partition(integer) SECURITY INVOKER;
