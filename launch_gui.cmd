@echo off
setlocal EnableExtensions
REM BatchArchive Houdini GUI launcher
REM Keep this file PURE ASCII + CRLF. No Chinese text here: the console code page
REM is 936 on some machines and 65001 on others, and no single non-ASCII encoding
REM stays readable on both. Chinese docs live in README.md / README.txt.
REM
REM WARNING: do not put python one-liners containing parentheses inside a for/f
REM that sits inside an if( ) block - the inner ( ) break the block parsing and
REM the whole probe silently returns nothing. That is what "where python" avoids.
cd /d "%~dp0"

set "PYTHON_EXE="
set "PYTHON_FALLBACK="

REM 1) Every interpreter on PATH (where lists one per line, no parsing needed).
where python >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%P in ('where python') do call :try_python "%%P"
)

REM 2) Per-user installs, newest first.
if not defined PYTHON_EXE (
    for %%D in (
        "%LocalAppData%\Programs\Python\Python313"
        "%LocalAppData%\Programs\Python\Python312"
        "%LocalAppData%\Programs\Python\Python311"
        "%LocalAppData%\Programs\Python\Python310"
    ) do if exist "%%~D\python.exe" call :try_python "%%~D\python.exe"
)

REM 3) Machine-wide installs.
if not defined PYTHON_EXE (
    for %%D in (
        "C:\Python313"
        "C:\Python312"
        "C:\Python311"
        "C:\Python310"
    ) do if exist "%%~D\python.exe" call :try_python "%%~D\python.exe"
)

REM Any interpreter beats none: take the first one found and install PyQt5 into it.
if not defined PYTHON_EXE set "PYTHON_EXE=%PYTHON_FALLBACK%"

if not defined PYTHON_EXE (
    echo [ERROR] Python 3 not found. Install it from https://www.python.org/downloads/
    goto :fail
)

echo Using Python: %PYTHON_EXE%

"%PYTHON_EXE%" -c "import PyQt5" >nul 2>nul
if errorlevel 1 (
    echo PyQt5 is missing, installing...
    "%PYTHON_EXE%" -m pip install PyQt5
    "%PYTHON_EXE%" -c "import PyQt5" >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] PyQt5 still cannot be imported after install. Run manually:
        echo         "%PYTHON_EXE%" -m pip install PyQt5
        goto :fail
    )
)

"%PYTHON_EXE%" "%~dp0app.py"
if errorlevel 1 (
    echo [ERROR] app.py exited with code %errorlevel%.
    goto :fail
)
exit /b 0

:fail
echo.
echo ---- diagnostics ----
echo script : %~f0
echo cwd    : %CD%
echo python : %PYTHON_EXE%
if defined PYTHON_EXE "%PYTHON_EXE%" -c "import sys;print('version:',sys.version.split()[0])"
if defined PYTHON_EXE "%PYTHON_EXE%" -c "import PyQt5" >nul 2>nul
if defined PYTHON_EXE if errorlevel 1 echo pyqt5  : MISSING
if defined PYTHON_EXE if not errorlevel 1 echo pyqt5  : ok
echo.
pause
exit /b 1

REM ---- helpers ----
REM Pick the first interpreter that can already import PyQt5, so we never end up
REM on a stray Python (an IDE bundled 3.13, the Store stub, ...) and die later.
:try_python
if defined PYTHON_EXE goto :eof
if not exist "%~1" goto :eof
if not defined PYTHON_FALLBACK set "PYTHON_FALLBACK=%~1"
"%~1" -c "import PyQt5" >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=%~1"
goto :eof
