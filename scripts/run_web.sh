#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${VIDEO_WORKSTATION_SESSION_SECRET:?set VIDEO_WORKSTATION_SESSION_SECRET first}"
export PYTHONPATH=src
exec .venv/bin/python -m uvicorn video_workstation.main:application --factory --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
