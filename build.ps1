# SPDX-License-Identifier: GPL-3.0-only
# 在 Windows 上构建单文件 exe：先建 .venv 并装 requirements.txt，再运行本脚本。
# 生成两个：RustSimHub-UsbTool.exe（图形界面）和 RustSimHub-UsbTool-cli.exe（控制台，命令行用时能看到输出）。
$ErrorActionPreference = 'Stop'
$py = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { throw 'tests failed' }
# 本机私有加载器（高通专有文件）：摘要相符才打进 exe。这样的 exe 只能自用，不能公开分发。文件本身不进 git。
$loader = Join-Path $PSScriptRoot '..\RustSimHub\.private\prog_emmc_firehose_8916.mbn'
$loaderSha = '959439AA5864685999B713C3ED12AD5FA408149648B670A9A9EF77BCC9DCAB14'
$extra = @()
if ((Test-Path $loader) -and (Get-FileHash $loader -Algorithm SHA256).Hash -eq $loaderSha) {
    $extra = @('--add-data', "$((Resolve-Path $loader).Path);loaders")
    Write-Output 'Bundling private Firehose loader: this exe is for personal use only.'
} else {
    Write-Output 'Private loader not found: building without a bundled loader.'
}
foreach ($variant in @(@{ Name = 'RustSimHub-UsbTool'; Mode = '--windowed' }, @{ Name = 'RustSimHub-UsbTool-cli'; Mode = '--console' })) {
    & $py -m PyInstaller --noconfirm --clean --onefile $variant.Mode --name $variant.Name `
        --collect-all edlclient --add-data "LICENSE;." --add-data "NOTICE.md;." @extra run_usbtool.py
    if ($LASTEXITCODE -ne 0) { throw 'pyinstaller failed' }
}
Get-FileHash dist\RustSimHub-UsbTool.exe, dist\RustSimHub-UsbTool-cli.exe -Algorithm SHA256
