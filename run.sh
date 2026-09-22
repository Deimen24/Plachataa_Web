#!/usr/bin/env bash
#
# Start the Plachataa Web server.
#
#   ./run.sh                 http://localhost:7870, opens your browser
#   ./run.sh --listen        also reachable from other devices on the LAN
#   ./run.sh --port 8000
#   ./run.sh --preload v1    load a model family at start
#   PLACHATAA_NO_BROWSER=1 ./run.sh   do not open a browser
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
[ -x "$ROOT/.venv/bin/python" ] || { echo "run ./install.sh first" >&2; exit 1; }

# Exported so torch loads the driver's CUDA libs first if both exist.
export PYTHONUNBUFFERED=1
[ -f "$ROOT/.env" ] && set -a && . "$ROOT/.env" && set +a

open_flag="--open"
[ "${PLACHATAA_NO_BROWSER:-0}" != 0 ] && open_flag=""

exec "$ROOT/.venv/bin/python" -m server.main $open_flag "$@"
