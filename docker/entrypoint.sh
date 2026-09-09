#!/bin/sh
set -e

# Compose uses one initialization service; workers never race migrations.
if [ "${DUAL_LOBE_INITIALIZE:-false}" = "true" ]; then
    alembic upgrade head
    python -m dual_lobe.core.bootstrap
fi
exec "$@"
