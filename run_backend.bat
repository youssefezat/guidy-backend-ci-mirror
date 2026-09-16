@echo off
REM Double-click this file to start the Guidy backend.
REM (PowerShell .ps1 files can't be double-clicked directly on Windows --
REM the default execution policy blocks it -- so this .bat is the real
REM entry point, and it launches the .ps1 with that policy bypassed just
REM for this one run.)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_backend.ps1"
