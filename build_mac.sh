#!/bin/bash
# 在 Mac 上打包：生成 dist/工程师排班管理.app
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/pyinstaller --noconfirm --clean --windowed \
  --name "工程师排班管理" \
  --add-data "ui:ui" \
  --collect-all playwright --collect-all webview \
  main.py
echo "完成：dist/工程师排班管理.app"
