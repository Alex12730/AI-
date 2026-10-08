#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
exec .venv/bin/python -m video_workstation.cli worker "$@"
