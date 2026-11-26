@echo off
cd /d "%~dp0"
title BtWriter Phone MCP
"python" "%~dp0phone_mcp_server.py" --http
