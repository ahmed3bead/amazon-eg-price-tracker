# Runs the tracker on this PC. Windows Task Scheduler starts it every 15 minutes.
# Telegram settings come from the user environment variables TELEGRAM_TOKEN and TELEGRAM_CHAT_ID.

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "data\run.log"
New-Item -ItemType Directory -Force (Join-Path $PSScriptRoot "data") | Out-Null

$env:TELEGRAM_TOKEN = [Environment]::GetEnvironmentVariable("TELEGRAM_TOKEN", "User")
$env:TELEGRAM_CHAT_ID = [Environment]::GetEnvironmentVariable("TELEGRAM_CHAT_ID", "User")
$env:DATA_DIR = Join-Path $PSScriptRoot "data"   # PC keeps its own history, separate from GitHub's
$env:DISCOVER_DROP = "25"
$env:LISTED_DISCOUNT = "50"
$env:PYTHONIOENCODING = "utf-8"

# Files shared with the GitHub backup: what to follow (edited by bot.py) and what was already sent.
$shared = "products.txt", "searches.txt", "sent.json", "settings.json", "ignored.txt"

function Save-Shared($message) {
    git add -- ($shared | Where-Object { Test-Path $_ }) 2>&1 | Out-Null
    git diff --cached --quiet
    if ($LASTEXITCODE -ne 0) {
        git -c user.name="price-bot-pc" -c user.email="price-bot@users.noreply.github.com" commit -q -m $message 2>&1 | Out-Null
    }
}

# Tell the GitHub backup to sit out while the PC is on duty.
gh variable set LAST_PC_RUN --body ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds()) 2>&1 | Out-Null

# Commit changes made from Telegram, then pick up edits made on GitHub and deals GitHub sent.
# "-X theirs" keeps this PC's version of any line both sides changed.
Save-Shared "Changes from Telegram (PC)"
git pull -q --rebase --autostash -X theirs 2>&1 | Out-Null

"===== $(Get-Date -Format 'yyyy-MM-dd HH:mm') =====" | Add-Content $log -Encoding utf8
& python tracker.py 2>&1 | ForEach-Object { "$_" } | Add-Content $log -Encoding utf8

# Share what this run sent, so the GitHub backup won't send it again.
Save-Shared "Sent deals (PC)"
git push -q 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    git pull -q --rebase --autostash -X theirs 2>&1 | Out-Null
    git push -q 2>&1 | Out-Null
}

# Keep the log small.
$lines = Get-Content $log -Encoding utf8
if ($lines.Count -gt 3000) { $lines | Select-Object -Last 2000 | Set-Content $log -Encoding utf8 }
