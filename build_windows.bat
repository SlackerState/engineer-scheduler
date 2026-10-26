@echo off
rem 在 Windows 上打包：生成 dist\工程师排班管理\工程师排班管理.exe
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (py -3 -m venv .venv || python -m venv .venv)
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\pyinstaller --noconfirm --clean --windowed ^
  --name "工程师排班管理" ^
  --add-data "ui;ui" ^
  --collect-all playwright --collect-all webview ^
  main.py
echo 完成：dist\工程师排班管理\工程师排班管理.exe
pause
