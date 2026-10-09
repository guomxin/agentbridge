"""Read-only PostgreSQL role checks shared by source preflight and queries."""
ROLE_SQL = """SELECT current_setting('transaction_read_only')='on' AS readonly,
 current_setting('transaction_isolation')='repeatable read' AS repeatable,
 current_setting('default_transaction_read_only')='on' AS default_readonly,
 (rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole) AS privileged,
 has_database_privilege(current_database(),'TEMP') AS temp,
 EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE CASE WHEN n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind='S'
 THEN has_sequence_privilege(c.oid,'USAGE') ELSE false END) AS sequence_usage,
 EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE CASE WHEN n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind='S'
 THEN has_sequence_privilege(c.oid,'UPDATE') ELSE false END) AS sequence_update,
 EXISTS (SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
 WHERE CASE WHEN n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','p','v','f') THEN
 (has_table_privilege(c.oid,'INSERT,UPDATE,DELETE,TRUNCATE') OR
 has_any_column_privilege(c.oid,'INSERT,UPDATE')) ELSE false END) AS writable
 FROM pg_catalog.pg_roles WHERE rolname=current_user"""
