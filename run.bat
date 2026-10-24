@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python。请先安装 Python 3.9+ 并勾选 "Add python.exe to PATH":
  echo        https://www.python.org/downloads/windows/
  pause
  exit /b 1
)
python -m rustflashkit
if errorlevel 1 pause
