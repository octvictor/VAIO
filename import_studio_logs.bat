@echo off
REM Double-click this to bring a Notion database into VAIO's Studio Logs.
REM You can also drag the export straight onto this file's icon.
REM
REM No venv and no pip here, unlike run.bat: the importer is deliberately
REM standard-library only, so it runs on whatever Python is installed and
REM there is nothing to set up before a one-off migration.
cd /d "%~dp0"
title VAIO - import Studio Logs

REM Windows installs Python either as "py" (the launcher, preferred) or as
REM "python". Checking both means this works on a machine that only has one.
set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (where python >nul 2>nul && set PY=python)
if not defined PY (
    echo.
    echo Python is not installed, or not on PATH.
    echo Get it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" in the installer, then run this again.
    echo.
    pause
    exit /b 1
)

REM %1 is the file or folder dropped onto this icon, empty on a plain
REM double-click - in which case the script asks for it.
%PY% tools\import_notion_studio_logs.py %1

echo.
pause
