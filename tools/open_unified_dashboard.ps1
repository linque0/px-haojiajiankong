$ErrorActionPreference='Stop'
$repositoryRoot=Split-Path -Parent $PSScriptRoot
$dashboardRoot=Join-Path $repositoryRoot 'data\analysis\valuation\wuwa-dashboard'
$bootstrapFile=Join-Path $dashboardRoot 'bootstrap.ps1'
if(!(Test-Path -LiteralPath $bootstrapFile)) { throw '未找到统一看板工程，请先恢复 wuwa-dashboard。' }
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $bootstrapFile -Action start -NoBrowser
if($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$dashboardConfig=Get-Content -LiteralPath (Join-Path $dashboardRoot 'local-config.json') -Raw | ConvertFrom-Json
$dashboardPort=$dashboardConfig.port
if($env:WUWA_DASHBOARD_PORT) { $dashboardPort=[int]$env:WUWA_DASHBOARD_PORT }
if($dashboardPort -lt 1024 -or $dashboardPort -gt 65535) { throw '看板端口配置无效。' }
Start-Process "http://127.0.0.1:$dashboardPort/?view=1&tab=operations"
