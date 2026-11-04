@echo off
setlocal EnableExtensions
REM BatchArchive Houdini GUI launcher
cd /d "%~dp0"

set "PYTHON_EXE="

REM 1) Prefer the official py launcher, which resolves any installed version.
where py >nul 2>nul
if not errorlevel 1 (
    for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do set "PYTHON_EXE=%%P"
)

REM 2) Fall back to python on PATH.
if not defined PYTHON_EXE (
    where python >nul 2>nul
    if not errorlevel 1 (
        for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable)" 2^>nul`) do set "PYTHON_EXE=%%P"
    )
)

REM 3) Last resort: this machine's per-user Python 3.11 install.
if not defined PYTHON_EXE if exist "%LocalAppData%\Programs\Python\Python311\python.exe" set "PYTHON_EXE=%LocalAppData%\Programs\Python\Python311\python.exe"

if not defined PYTHON_EXE (
    echo 未找到可用的 Python，请先安装 Python 3（https://www.python.org/downloads/）。
    pause
    exit /b 1
)

echo 使用 Python：%PYTHON_EXE%

"%PYTHON_EXE%" -c "import PyQt5" >nul 2>nul
if errorlevel 1 (
    echo 缺少 PyQt5 依赖，正在尝试安装...
    "%PYTHON_EXE%" -m pip install PyQt5
    if errorlevel 1 (
        echo PyQt5 安装失败，请手动执行："%PYTHON_EXE%" -m pip install PyQt5
        pause
        exit /b 1
    )
)

"%PYTHON_EXE%" "%~dp0app.py"
if errorlevel 1 pause
