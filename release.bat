@echo off
REM Double-click this to publish a new version of VAIO.
REM
REM It tags the current code and pushes the tag, which is what makes
REM GitHub build VAIO.exe and put it on the releases page. It asks before
REM anything is published, and suggests the next version number so there
REM is nothing to remember.
cd /d "%~dp0"
title VAIO - publish a release

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

%PY% tools\release.py

echo.
pause
