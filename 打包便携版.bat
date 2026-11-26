@echo off
setlocal
cd /d "%~dp0"
"python" -m PyInstaller --noconfirm --clean BtWriterPhone.spec
if errorlevel 1 (
  echo.
  echo 打包失败，请检查上面的 PyInstaller 错误。
  pause
  exit /b 1
)
"python" -c "import json,pathlib; src=pathlib.Path('tapper_config.json'); cfg=json.loads(src.read_text(encoding='utf-8')); cfg['port']='auto'; cfg['camera_library_path']=''; out=pathlib.Path('dist/BtWriterPhone/tapper_config.json'); out.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')"
copy /y "%~dp0启动便携版.bat" "%~dp0dist\BtWriterPhone\启动便携版.bat" >nul
copy /y "%~dp0便携版说明.md" "%~dp0dist\BtWriterPhone\使用说明.md" >nul
copy /y "%~dp0Codex MCP配置.toml" "%~dp0dist\BtWriterPhone\Codex MCP配置.toml" >nul
copy /y "%~dp0配置Codex MCP.bat" "%~dp0dist\BtWriterPhone\配置Codex MCP.bat" >nul
copy /y "%~dp0配置Codex MCP.ps1" "%~dp0dist\BtWriterPhone\配置Codex MCP.ps1" >nul
if exist "%~dp0dist\BtWriterPhone-便携版.zip" del "%~dp0dist\BtWriterPhone-便携版.zip"
powershell -NoProfile -Command "Compress-Archive -Path '%~dp0dist\BtWriterPhone\*' -DestinationPath '%~dp0dist\BtWriterPhone-便携版.zip' -CompressionLevel Optimal"
echo.
echo 便携版已生成：%~dp0dist\BtWriterPhone
echo 可复制文件夹或发送 ZIP：%~dp0dist\BtWriterPhone-便携版.zip
pause
