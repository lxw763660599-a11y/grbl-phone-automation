@echo off
cd /d "%~dp0"
title BtWriter Grbl Control Service
"python" grbl_service.py
pause
