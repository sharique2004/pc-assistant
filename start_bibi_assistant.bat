@echo off
setlocal
cd /d "%~dp0backend"

set "PYTHON_EXE="
set "SITE_PACKAGES=%CD%\.venv\Lib\site-packages"

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; print(sys.version)" >nul 2>nul
  if "%errorlevel%"=="0" set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
)

if not defined PYTHON_EXE if exist "%USERPROFILE%\Desktop\Iterenary\myenv\Scripts\python.exe" set "PYTHON_EXE=%USERPROFILE%\Desktop\Iterenary\myenv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\Desktop\Portfolio\.venv\Scripts\python.exe" set "PYTHON_EXE=%USERPROFILE%\Desktop\Portfolio\.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%USERPROFILE%\Documents\Myinfo\myenv\Scripts\python.exe" set "PYTHON_EXE=%USERPROFILE%\Documents\Myinfo\myenv\Scripts\python.exe"

if not defined PYTHON_EXE (
  echo Could not find a working Python runtime for Bibi.
  pause
  exit /b 1
)

powershell.exe -NoProfile -Command "$existing = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -like '*desktop_agent.py*' }; if ($existing) { exit 0 } else { exit 1 }"
if "%errorlevel%"=="0" (
  exit /b 0
)

set "PYTHONPATH=%SITE_PACKAGES%;%SITE_PACKAGES%\win32;%SITE_PACKAGES%\win32\lib;%SITE_PACKAGES%\Pythonwin;%PYTHONPATH%"
set "PATH=%SITE_PACKAGES%\pywin32_system32;%PATH%"

start "" powershell.exe -NoProfile -WindowStyle Hidden -Command "$site = '%SITE_PACKAGES%'; Set-Location -LiteralPath '%CD%'; $env:PYTHONPATH = '%PYTHONPATH%'; $env:PATH = '%PATH%'; & '%PYTHON_EXE%' -c \"import os, runpy; os.add_dll_directory(r'$site\pywin32_system32'); runpy.run_path('desktop_agent.py', run_name='__main__')\""
exit /b 0
