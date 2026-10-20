@echo off
title Graduation Study Hub

:: Change directory to the project folder (handles drive changes with /d)
cd /d "E:\#My University\MSA\Study Hub"

:: Optional: Silence PyWebView C++ native logging warnings in the terminal
set PYWEBVIEW_LOG=error

echo Starting Graduation Study Hub...
python server.py

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Application closed with an error.
    pause
)