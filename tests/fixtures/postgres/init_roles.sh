set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$GATEWAY_DB_PASSWORD" \
  --set=restricted_password="$GATEWAY_DB_PASSWORD" <<'SQL'
CREATE ROLE gateway_test LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
GRANT CONNECT ON DATABASE mcpcodeaway_acceptance TO gateway_test;
CREATE SCHEMA gateway AUTHORIZATION gateway_test;
ALTER ROLE gateway_test SET search_path TO gateway, public;
CREATE ROLE gateway_limited LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'restricted_password';
GRANT CONNECT ON DATABASE mcpcodeaway_acceptance TO gateway_limited;
CREATE SCHEMA restricted AUTHORIZATION acceptance_bootstrap;
GRANT USAGE ON SCHEMA restricted TO gateway_limited;
ALTER ROLE gateway_limited SET search_path TO restricted, public;
SQL
