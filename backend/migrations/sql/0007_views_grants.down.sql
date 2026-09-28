ALTER DEFAULT PRIVILEGES IN SCHEMA config REVOKE ALL ON TABLES FROM app_api, app_ingest, app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA core REVOKE ALL ON TABLES FROM app_api, app_ingest, app_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA ingest REVOKE ALL ON TABLES FROM app_api, app_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth REVOKE ALL ON TABLES FROM app_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA analyst REVOKE ALL ON TABLES FROM app_api;
ALTER DEFAULT PRIVILEGES IN SCHEMA core, ingest REVOKE ALL ON SEQUENCES FROM app_ingest;
ALTER DEFAULT PRIVILEGES IN SCHEMA auth, analyst REVOKE ALL ON SEQUENCES FROM app_api;

ALTER ROLE app_readonly RESET statement_timeout;
ALTER ROLE app_readonly RESET default_transaction_read_only;

REVOKE ALL ON ALL TABLES IN SCHEMA config, core, ingest, auth, analyst FROM app_api, app_ingest, app_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA config, core, ingest, auth, analyst FROM app_api, app_ingest;
REVOKE USAGE ON SCHEMA util, config, core, ingest, auth, analyst FROM app_api, app_ingest, app_readonly;

DROP FUNCTION IF EXISTS util.refresh_stat_views();
DROP MATERIALIZED VIEW IF EXISTS core.mv_team_season_stat_long;
DROP MATERIALIZED VIEW IF EXISTS core.mv_player_season_stat_long;
