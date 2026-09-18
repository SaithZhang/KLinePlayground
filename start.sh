#!/bin/sh
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "请先建立 .venv 并安装 requirements-web.txt，详见 docs/55_TRAINING.md" >&2
    exit 1
fi
exec .venv/bin/python main_enhanced.py
