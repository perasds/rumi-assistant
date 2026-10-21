[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$FrameworkOnly,
    [ValidateRange(1024, 65535)][int]$ApiPort = 8000,
    [ValidateRange(1024, 65535)][int]$WebPort = 5173
)

$ErrorActionPreference = 'Stop'
$projectDirectory = $PSScriptRoot
$runtimeDirectory = Join-Path $projectDirectory 'runtime'
$stateFile = Join-Path $runtimeDirectory 'services.json'

function Get-OwnedProcess([int]$TaskProcessId) {
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $TaskProcessId"
    if ($candidate -and $candidate.CommandLine -and
        $candidate.CommandLine -match ([regex]::Escape($projectDirectory) + '(?:[\\/"\s]|$)')) {
        return $candidate
    }
    return $null
}

function Stop-OwnedProcessTree([int]$TaskProcessId) {
    if (-not (Get-OwnedProcess $TaskProcessId)) { return }
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $TaskProcessId"
    foreach ($child in $children) { Stop-OwnedProcessTree $child.ProcessId }
    if (Get-OwnedProcess $TaskProcessId) { Stop-Process -Id $TaskProcessId -ErrorAction SilentlyContinue }
}

if ($Stop) {
    if (Test-Path -LiteralPath $stateFile) {
        $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
        foreach ($taskProcessId in @($saved.api_launcher_pid, $saved.api_pid, $saved.web_launcher_pid, $saved.web_pid)) {
            if ($taskProcessId) { Stop-OwnedProcessTree $taskProcessId }
        }
        Remove-Item -LiteralPath $stateFile
    }
    Write-Output 'Rumi Market services stopped.'
    return
}

if ($ApiPort -eq $WebPort) { throw 'API and web ports must differ.' }
foreach ($taskPort in @($ApiPort, $WebPort)) {
    if (Get-NetTCPConnection -LocalPort $taskPort -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $taskPort is already in use. Stop the existing service or select another port."
    }
}

$pythonExecutable = Join-Path $projectDirectory '.venv\Scripts\python.exe'
$nodeExecutable = $env:RUMI_NODE_EXE
if (-not $nodeExecutable) {
    $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
    if ($nodeCommand) { $nodeExecutable = $nodeCommand.Source }
}
$viteScript = Join-Path $projectDirectory 'frontend\node_modules\vite\bin\vite.js'
$backendSource = Join-Path $projectDirectory 'backend\src'
$apiApplication = if ($FrameworkOnly) { 'rumi_market.app:app' } else { 'rumi_market.live_app:app' }
foreach ($required in @($pythonExecutable, $nodeExecutable, $viteScript)) {
    if (-not $required -or -not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw 'Dependencies are missing. Follow README setup steps; RUMI_NODE_EXE may specify Node.js.'
    }
}

$preflightArguments = @()
if ($FrameworkOnly) { $preflightArguments += '--framework-only' }
& $pythonExecutable (Join-Path $projectDirectory 'scripts\check_materials.py') @preflightArguments
if ($LASTEXITCODE -ne 0) { throw 'Required local materials are missing or invalid. See the preflight report above.' }

New-Item -ItemType Directory -Path $runtimeDirectory -Force | Out-Null
$apiProcess = $null
$webProcess = $null
$previousTarget = $env:RUMI_API_TARGET
$previousPath = $env:Path
try {
    $env:Path = (Split-Path -Parent (Resolve-Path -LiteralPath $nodeExecutable).Path) + [IO.Path]::PathSeparator + $previousPath
    $apiProcess = Start-Process -FilePath $pythonExecutable -WorkingDirectory $projectDirectory -WindowStyle Hidden -PassThru `
        -ArgumentList @('-m', 'uvicorn', $apiApplication, '--app-dir', ('"' + $backendSource + '"'), '--host', '127.0.0.1', '--port', $ApiPort) `
        -RedirectStandardOutput (Join-Path $runtimeDirectory 'api-stdout.log') -RedirectStandardError (Join-Path $runtimeDirectory 'api-stderr.log')
    $env:RUMI_API_TARGET = "http://127.0.0.1:$ApiPort"
    $webProcess = Start-Process -FilePath $nodeExecutable -WorkingDirectory (Join-Path $projectDirectory 'frontend') -WindowStyle Hidden -PassThru `
        -ArgumentList @(('"' + $viteScript + '"'), '--host', '127.0.0.1', '--port', $WebPort, '--strictPort') `
        -RedirectStandardOutput (Join-Path $runtimeDirectory 'web-stdout.log') -RedirectStandardError (Join-Path $runtimeDirectory 'web-stderr.log')
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        Start-Sleep -Milliseconds 250
        $apiProcess.Refresh()
        $webProcess.Refresh()
        if ($apiProcess.HasExited -or $webProcess.HasExited) { throw 'A service exited; inspect runtime logs.' }
        try {
            $health = Invoke-RestMethod "http://127.0.0.1:$ApiPort/api/health" -TimeoutSec 1
            $webStatus = Invoke-WebRequest "http://127.0.0.1:$WebPort/" -UseBasicParsing -TimeoutSec 1
            if ($health.service -eq 'rumi-market' -and $webStatus.StatusCode -eq 200) { $ready = $true; break }
        } catch { continue }
    }
    if (-not $ready) { throw 'Service readiness timed out; inspect runtime logs.' }
    $apiListener = Get-NetTCPConnection -LocalPort $ApiPort -State Listen | Select-Object -First 1
    $webListener = Get-NetTCPConnection -LocalPort $WebPort -State Listen | Select-Object -First 1
    if (-not (Get-OwnedProcess $apiListener.OwningProcess) -or -not (Get-OwnedProcess $webListener.OwningProcess)) {
        throw 'Service listener process ownership could not be verified.'
    }
    @{ api_pid = $apiListener.OwningProcess; web_pid = $webListener.OwningProcess; api_launcher_pid = $apiProcess.Id; web_launcher_pid = $webProcess.Id; api_port = $ApiPort; web_port = $WebPort } |
        ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding utf8
    Write-Output "Web: http://127.0.0.1:$WebPort/"
    Write-Output "API: http://127.0.0.1:$ApiPort/"
    Write-Output "Game integration ready: $($health.game_integration_ready)"
} catch {
    if ($webProcess) { Stop-OwnedProcessTree $webProcess.Id }
    if ($apiProcess) { Stop-OwnedProcessTree $apiProcess.Id }
    throw
} finally {
    $env:RUMI_API_TARGET = $previousTarget
    $env:Path = $previousPath
}
