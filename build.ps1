# SPDX-License-Identifier: GPL-3.0-only
# 在 Windows 上构建单文件 exe：先建 .venv 并装 requirements.txt，再运行本脚本。
$ErrorActionPreference = 'Stop'
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'tests failed' }
& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name RustSimHub-UsbTool `
    --collect-all edlclient --add-data "LICENSE;." --add-data "NOTICE.md;." run_usbtool.py
if ($LASTEXITCODE -ne 0) { throw 'pyinstaller failed' }
Get-FileHash dist\RustSimHub-UsbTool.exe -Algorithm SHA256
