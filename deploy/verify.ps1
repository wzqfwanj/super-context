# External acceptance checks for https://wkeeli.cn/context/ (run after go-live).
# Usage (Windows PowerShell):
#   powershell -ExecutionPolicy Bypass -File deploy/verify.ps1
#   powershell -ExecutionPolicy Bypass -File deploy/verify.ps1 -Credential "wkeeli:PASSWORD"
#   powershell -ExecutionPolicy Bypass -File deploy/verify.ps1 -Credential "wkeeli:PASSWORD" -UploadTest
#
# What it checks:
#   1. dev service healthz directly (via ssh dev)                -> {"ok":true}
#   2. public /context/ requires Basic Auth (expect 401)         -> auth gate alive
#   3. public /context/ with -Credential returns 200 + <html>    -> page served
#   4. public agent link /context/s/<token>/index.md without auth-> 200 + markdown
#   5. optional upload round-trip (needs -Credential -UploadTest) -> item created then deleted
#
# Notes: -Credential is the site-wide Basic Auth (same one used by https://wkeeli.cn/).
#        The agent token is NOT needed on the command line: it is read from the dev host
#        (/opt/super-context/data/items.json) over ssh.
param(
    [string]$Base = "https://wkeeli.cn/context",
    [string]$Credential = "",
    [switch]$UploadTest
)
$ErrorActionPreference = "Stop"
$script:Failures = 0

function Check([string]$Name, [bool]$Ok, [string]$Detail) {
    if ($Ok) {
        Write-Host ("[PASS] {0}  {1}" -f $Name, $Detail) -ForegroundColor Green
    } else {
        Write-Host ("[FAIL] {0}  {1}" -f $Name, $Detail) -ForegroundColor Red
        $script:Failures++
    }
}

function HttpCode([string[]]$CurlArgs) {
    $code = & curl.exe -s -o NUL -w "%{http_code}" --max-time 30 @CurlArgs 2>$null
    if (-not $code) { $code = "000" }
    return [int]$code
}

function HttpBody([string[]]$CurlArgs) {
    return (& curl.exe -s --max-time 30 @CurlArgs 2>$null) -join "`n"
}

$Base = $Base.TrimEnd('/')
$auth = @()
if ($Credential) { $auth = @("-u", $Credential) }

Write-Host "== base: $Base"

# 1) dev service on the host
$devHealth = (& ssh dev "curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8082/healthz" 2>$null) -join ""
Check "dev healthz 127.0.0.1:8082" ($devHealth -eq "200") "http=$devHealth"

# 2) auth gate on public page
$codeNoAuth = HttpCode (@("$Base/"))
Check "public page without auth -> 401" ($codeNoAuth -eq 401) "http=$codeNoAuth"

# 3) page with site-wide Basic Auth
if ($Credential) {
    $codeAuth = HttpCode ($auth + @("$Base/"))
    $body = HttpBody ($auth + @("$Base/"))
    $hasHtml = $body -match "<!DOCTYPE html|<html"
    Check "public page with auth -> 200" ($codeAuth -eq 200) "http=$codeAuth"
    Check "page body looks like html" $hasHtml ""
} else {
    Write-Host "[SKIP] public page with auth (pass -Credential user:pass)" -ForegroundColor Yellow
}

# 4) agent link without auth
$itemsJson = (ssh dev "cat /opt/super-context/data/items.json" 2>$null) -join "`n"
$token = ""
try { $token = [string](($itemsJson | ConvertFrom-Json).token) } catch { $token = "" }
$token = $token.Trim()
if ($token -match '^[0-9a-f]{32}$') {
    $code = HttpCode (@("$Base/s/$token/index.md"))
    Check "agent index.md without auth -> 200" ($code -eq 200) "http=$code token=$($token.Substring(0,4))***"
    if ($code -eq 200) {
        $md = HttpBody @("$Base/s/$token/index.md")
        Check "index.md is markdown list" ($md -match "# ") ""
    }
    $bad = HttpCode @("$Base/s/00000000000000000000000000000000/index.md")
    Check "wrong token -> 404" ($bad -eq 404) "http=$bad"
} else {
    Write-Host "[SKIP] agent link checks (could not read token from dev host)" -ForegroundColor Yellow
}

# 5) optional upload round-trip
if ($UploadTest -and $Credential) {
    $tmp = Join-Path $env:TEMP ("sc-verify-" + [guid]::NewGuid().ToString("N").Substring(0, 8) + ".txt")
    Set-Content -Path $tmp -Value "super-context deploy verification $(Get-Date -Format o)" -Encoding UTF8
    try {
        $resp = HttpBody ($auth + @("-X", "POST", "--data-binary", "@$tmp", "$Base/api/items?name=deploy-verify.txt"))
        $itemId = ""
        try { $itemId = ($resp | ConvertFrom-Json).item.id } catch { }
        Check "upload txt -> 201 + id" ($itemId -match '^[0-9a-f]{8}$') "id=$itemId resp=$($resp.Substring(0, [Math]::Min(120, $resp.Length)))"
        if ($itemId -match '^[0-9a-f]{8}$') {
            Start-Sleep -Seconds 2
            $del = HttpCode ($auth + @("-X", "DELETE", "$Base/api/items/$itemId"))
            Check "cleanup delete item" ($del -eq 200) "http=$del"
        }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "[SKIP] upload round-trip (pass -Credential and -UploadTest)" -ForegroundColor Yellow
}

Write-Host ""
if ($script:Failures -eq 0) {
    Write-Host "ALL CHECKS PASSED" -ForegroundColor Green
    exit 0
} else {
    Write-Host "$($script:Failures) CHECK(S) FAILED" -ForegroundColor Red
    exit 1
}
