#!/bin/sh
# Runs a private Postgres inside this container (data on the /data volume, listening on
# localhost only), then starts the unchanged pre-CrewAI app: migrations, key bootstrap,
# the B worker, and the API.
set -e

PGDATA="${PGDATA:-/data/pgdata}"
PGBIN="$(ls -d /usr/lib/postgresql/*/bin | head -1)"

mkdir -p "$PGDATA"
chown -R postgres:postgres "$PGDATA"
chmod 700 "$PGDATA"
if [ ! -s "$PGDATA/PG_VERSION" ]; then
    su postgres -c "$PGBIN/initdb -D $PGDATA -U postgres --auth=trust" >/dev/null
fi
su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o \"-c listen_addresses=127.0.0.1 -p 5432\" -l $PGDATA/server.log -w start"
su postgres -c "psql -h 127.0.0.1 -U postgres -tAc \"SELECT 1 FROM pg_database WHERE datname='dual_lobe'\"" | grep -q 1 \
    || su postgres -c "psql -h 127.0.0.1 -U postgres -c 'CREATE DATABASE dual_lobe'"

export DATABASE_URL="postgresql+asyncpg://postgres@127.0.0.1:5432/dual_lobe"
export RLS_DATABASE_URL="postgresql+asyncpg://dual_lobe_rls:dual_lobe_rls@127.0.0.1:5432/dual_lobe"

alembic upgrade head
python -m dual_lobe.core.bootstrap
python -m dual_lobe.b.worker &
exec uvicorn dual_lobe.api.app:app --host 0.0.0.0 --port "${PORT:-8000}"
