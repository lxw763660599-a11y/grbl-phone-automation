$ErrorActionPreference = 'Stop'
$configPath = Join-Path $env:USERPROFILE '.codex\config.toml'
$configDir = Split-Path -Parent $configPath
New-Item -ItemType Directory -Path $configDir -Force | Out-Null

if (Test-Path -LiteralPath $configPath) {
    $content = [System.IO.File]::ReadAllText($configPath)
} else {
    $content = ''
}

$block = "[mcp_servers.btwriter_phone]`nurl = `"http://127.0.0.1:8769/mcp`"`n"
$pattern = '(?ms)^\[mcp_servers\.btwriter_phone\]\r?\n.*?(?=^\[|\z)'
if ([regex]::IsMatch($content, $pattern)) {
    $content = [regex]::Replace($content, $pattern, $block, 1)
} else {
    if ($content.Length -gt 0 -and -not $content.EndsWith("`n")) {
        $content += "`n"
    }
    if ($content.Length -gt 0) {
        $content += "`n"
    }
    $content += $block
}

[System.IO.File]::WriteAllText($configPath, $content, [System.Text.UTF8Encoding]::new($false))
Write-Host "Codex MCP 已配置：$configPath"
Write-Host '重启 Codex 后即可调用 BtWriter 手机触控工具。'
