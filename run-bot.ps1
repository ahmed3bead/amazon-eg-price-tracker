# Keeps bot.py (the Telegram command listener) running. Task Scheduler starts this at log on.

Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "data") | Out-Null
$env:TELEGRAM_TOKEN = [Environment]::GetEnvironmentVariable("TELEGRAM_TOKEN", "User")
$env:TELEGRAM_CHAT_ID = [Environment]::GetEnvironmentVariable("TELEGRAM_CHAT_ID", "User")
$env:DATA_DIR = Join-Path $PSScriptRoot "data"
$env:PYTHONIOENCODING = "utf-8"
$log = Join-Path $PSScriptRoot "data\bot.log"

while ($true) {
    & python bot.py 2>&1 | ForEach-Object { "$(Get-Date -Format 'yyyy-MM-dd HH:mm') $_" } | Add-Content $log -Encoding utf8
    Start-Sleep -Seconds 30  # crashed or lost the network; try again shortly
}
