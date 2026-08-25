@echo off
setlocal
REM BatchArchive Houdini GUI launcher
cd /d "%~dp0"
if exist "C:\Users\Gaole\AppData\Local\Programs\Python\Python311\python.exe" goto launch_python311
echo 未找到 Python 3.11：C:\Users\Gaole\AppData\Local\Programs\Python\Python311\python.exe
pause
exit /b 1

:launch_python311
"C:\Users\Gaole\AppData\Local\Programs\Python\Python311\python.exe" "%~dp0app.py"
if errorlevel 1 pause
