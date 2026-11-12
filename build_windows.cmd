@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where cargo >nul 2>nul
if errorlevel 1 (
    echo 未找到 cargo，请先安装 Rust 工具链：https://rustup.rs
    pause
    exit /b 1
)

echo 正在编译 release 版本...
cargo build --release
if errorlevel 1 (
    echo 编译失败。
    pause
    exit /b 1
)

if not exist dist mkdir dist
copy /Y "target\release\batcharchive.exe" "dist\BatchArchive.exe" >nul
if exist scripts (
    if not exist "dist\scripts" mkdir "dist\scripts"
    copy /Y "scripts\package_houdini.py" "dist\scripts\package_houdini.py" >nul
)

echo.
echo 输出：dist\BatchArchive.exe
echo 说明：scripts\package_houdini.py 可选；缺失时使用 exe 内嵌副本。
pause
