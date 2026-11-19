#!/usr/bin/env bash
# Start (creating if needed) a local Postgres 16 with pgvector on port 5433 for tests.
# Needs: apt install postgresql-16 postgresql-16-pgvector. Run as root; the server runs as `postgres`.
set -euo pipefail
DIR=/tmp/pg
BIN=/usr/lib/postgresql/16/bin
mkdir -p "$DIR" && chown postgres "$DIR"
if [ ! -d "$DIR/data" ]; then su postgres -c "$BIN/initdb -D $DIR/data -A trust >/dev/null"; fi
if ! pg_isready -q -h localhost -p 5433; then
  su postgres -c "$BIN/pg_ctl -D $DIR/data -o '-p 5433 -k $DIR' -l $DIR/log -w start" >/dev/null
fi
for db in microdrama microdrama_test; do
  su postgres -c "psql -h $DIR -p 5433 -tc \"select 1 from pg_database where datname='$db'\"" | grep -q 1 \
    || su postgres -c "psql -h $DIR -p 5433 -c 'create database $db'" >/dev/null
done
su postgres -c "psql -h $DIR -p 5433 -d microdrama_test -c 'create extension if not exists vector'" >/dev/null
echo "postgres ready on localhost:5433"
