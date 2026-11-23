# Sync this repo to the dev host (/opt/super-context) and restart the super-context service.
# Usage (Windows PowerShell):
#   powershell -ExecutionPolicy Bypass -File deploy/push.ps1
# Requires ~/.ssh/config `Host dev`; dev host must be prepared once (see deploy/README.md).
# Never overwrites runtime data (/opt/super-context/data) or secrets (/opt/super-context/.env).
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Remote = "/opt/super-context"

$Required = @("server.py", "describe.py")
foreach ($f in $Required) {
    if (-not (Test-Path (Join-Path $Root $f))) {
        throw "missing required file: $f -- repo not ready yet (service would crash without it)"
    }
}
if (-not (Test-Path (Join-Path $Root "web"))) {
    throw "missing required dir: web/"
}

Write-Host "==> create remote dirs"
ssh dev "mkdir -p $Remote/web $Remote/deploy $Remote/data/files"

Write-Host "==> upload server"
scp -q "$Root/server.py" "dev:${Remote}/server.py"
scp -q "$Root/describe.py" "dev:${Remote}/describe.py"
if (Test-Path "$Root/test_server.py") {
    scp -q "$Root/test_server.py" "dev:${Remote}/test_server.py"
}
if (Test-Path "$Root/test_describe.py") {
    scp -q "$Root/test_describe.py" "dev:${Remote}/test_describe.py"
}

Write-Host "==> upload web"
scp -q -r "$Root/web/." "dev:${Remote}/web/"

Write-Host "==> upload deploy"
scp -q -r "$Root/deploy/." "dev:${Remote}/deploy/"

Write-Host "==> restart service"
ssh dev "systemctl restart super-context"
Start-Sleep -Seconds 2
Write-Host ("    is-active: " + (ssh dev "systemctl is-active super-context"))
Write-Host "==> health check"
ssh dev "curl -s http://127.0.0.1:8082/healthz"
Write-Host ""
Write-Host "done: runtime /opt/super-context/data and /opt/super-context/.env were NOT overwritten."
