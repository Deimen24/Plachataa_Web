@echo off
rem Remove Plachataa Web. Options: -KeepData -Purge -RemoveDir -Yes
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
pause
