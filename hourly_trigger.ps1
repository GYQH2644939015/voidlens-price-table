# VoidLens price table - local fallback trigger (hourly dispatch)
#
# Called by Windows Task Scheduler task "VoidLensPriceTrigger" at minute :22 of every hour.
# It only POSTs one workflow_dispatch. It does not fetch prices, does not git push.
#
# Why not git push: with the proxy off, github.com:443 times out (21s measured),
# while api.github.com connects in 0.6-0.8s. Pushing would just pile up failures locally.
#
# Run manually: powershell -NoProfile -ExecutionPolicy Bypass -File hourly_trigger.ps1
# See result:   Get-Content trigger.log -Tail 10
# Delete task:  schtasks /Delete /TN VoidLensPriceTrigger /F

$ErrorActionPreference = 'Stop'

$root     = 'D:\Backup\Documents\deepseek-harness\default-workspace'
$patFile  = Join-Path $root '.secrets\pat.txt'
$logFile  = Join-Path $root 'voidlens-price-table\trigger.log'
$repo     = 'GYQH2644939015/voidlens-price-table'
$workflow = 'price-publish.yml'
$stamp    = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

function Write-Log($text) {
    $line = "[$stamp] $text"
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Output $line
}

try {
    if (-not (Test-Path $patFile)) { throw "PAT file not found: $patFile" }
    $token = ((Get-Content -Raw $patFile) -replace '^\s*GH_TOKEN\s*=\s*', '').Trim()
    if (-not $token) { throw 'PAT file is empty' }

    $uri = "https://api.github.com/repos/$repo/actions/workflows/$workflow/dispatches"
    $headers = @{
        Authorization = "Bearer $token"
        Accept = 'application/vnd.github+json'
        'User-Agent' = 'voidlens-pc-cron/1.0'
    }
    # no "force" input on purpose: the workflow already dedupes within 40 minutes.
    # "via" is only recorded into publish_status.json so we can tell which trigger woke it.
    $body = '{"ref":"main","inputs":{"via":"pc"}}'

    $resp = Invoke-WebRequest -Method Post -Uri $uri -Headers $headers -Body $body -ContentType 'application/json' -UseBasicParsing -TimeoutSec 30
    if ($resp.StatusCode -eq 204) {
        Write-Log 'OK dispatched (204)'
    } else {
        Write-Log "WARN unexpected status $($resp.StatusCode)"
    }
} catch {
    # 401 = token expired, 404 = wrong repo/workflow, anything else = network
    Write-Log "FAIL $($_.Exception.Message)"
    exit 1
}
