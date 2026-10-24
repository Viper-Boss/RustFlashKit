@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python，请先安装 Python 3.9+
  pause
  exit /b 1
)
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :err
python -m PyInstaller --noconfirm --clean rustflashkit.spec
if errorlevel 1 goto :err
echo.
echo 打包完成: dist\RustFlashKit\RustFlashKit.exe
echo 使用前请把 adb.exe / fastboot.exe 放到 dist\RustFlashKit\tools\ 目录
pause
exit /b 0
:err
echo 打包失败，请检查上方错误信息
pause
exit /b 1
