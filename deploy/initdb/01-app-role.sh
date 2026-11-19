#!/bin/sh
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Mykhailo Zvir
# Runs once, on the first start of an empty Postgres volume.
# Creates the restricted role the web process uses: not superuser, no BYPASSRLS, owns nothing.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v "app_password=${STO_APP_DB_PASSWORD}" <<'SQL'
CREATE ROLE sto_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD :'app_password';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL
