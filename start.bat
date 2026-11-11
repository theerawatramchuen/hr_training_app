@echo off
rem ============================================================
rem  TRACKR - Start training sessions
rem  Double-click this file in File Explorer to run it.
rem ============================================================
setlocal
title TRACKR - update training database

rem Folder with trackr_db.py, trackr_sessions and the "env" virtual environment
set "APP_DIR=D:\hr_training"
set "PY=%APP_DIR%\env\Scripts\python.exe"

cd /d "%APP_DIR%" 2>nul || (
    echo ERROR: folder not found: %APP_DIR%
    goto end
)
if not exist "%PY%" (
    echo ERROR: Python virtual environment not found: %PY%
    goto end
)

echo === Starting Training ===
"%PY%" trackr_hr.py 

:end
echo.

