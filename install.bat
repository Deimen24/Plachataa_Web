@echo off
rem Plachataa Web installer (Windows). Pass the same switches as install.ps1,
rem e.g.  install.bat -DownloadModels -UpdateDriver
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
if errorlevel 1 pause
