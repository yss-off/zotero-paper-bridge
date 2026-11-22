<#
.SYNOPSIS
  Start local Zotero Paper Bridge helper on Windows, establish private reverse SSH
  Unix-socket tunnels, and auto-drain any queued papers on the remote Linux server.

.EXAMPLE
  .\start-paper-bridge.ps1 -SshHost my-gpu-server
  .\start-paper-bridge.ps1 -SshHost user@10.0.0.10 -UseTcpFallback
#>
param(
    [Parameter(Mandatory = $true)]
    [string]$SshHost,

    [string]$RemoteSocketPath = "",
    [string]$RemoteZoteroReadSocketPath = "",
    [int]$HelperPort = 18765,
    [int]$ZoteroRoProxyPort = 23120,
    [switch]$UseTcpFallback,
    [switch]$UseTcpZotero23119,
    [switch]$NoZoteroReadBack,
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$RootDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$env:PYTHONPATH = "$RootDir"

$TokenPath = Join-Path $env:USERPROFILE ".paper-bridge\token"
if (-not (Test-Path $TokenPath)) {
    Write-Host "[ERROR] Token file not found: $TokenPath" -ForegroundColor Red
    Write-Host "Copy the token from your remote server (~/.config/paper-bridge/token) by running:"
    Write-Host "  $PythonExe -m paper_bridge.client.cli init --token <YOUR_TOKEN>"
    exit 1
}

# Lock down Windows token ACL to current user only
try {
    icacls $TokenPath /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
} catch {
    Write-Host "[WARN] Could not tighten ACL on $TokenPath; please check file permissions." -ForegroundColor Yellow
}

# Start helper in background if not already listening on $HelperPort
$helperAlive = $false
try {
    $tcp = New-Object System.Net.Sockets.TcpClient("127.0.0.1", $HelperPort)
    $tcp.Close()
    $helperAlive = $true
    Write-Host "[OK] paper-bridge-helper is already running on 127.0.0.1:$HelperPort" -ForegroundColor Green
} catch {
    $helperAlive = $false
}

$helperProc = $null
if (-not $helperAlive) {
    Write-Host "[INFO] Starting paper-bridge-helper on 127.0.0.1:$HelperPort ..."
    $helperProc = Start-Process -FilePath $PythonExe `
        -ArgumentList @("-m", "paper_bridge.client.cli", "serve") `
        -WorkingDirectory $RootDir `
        -WindowStyle Minimized `
        -PassThru
    Start-Sleep -Seconds 2
}

$needRemoteHome = ((-not $UseTcpFallback) -and (-not $RemoteSocketPath)) -or ((-not $NoZoteroReadBack) -and (-not $UseTcpZotero23119) -and (-not $RemoteZoteroReadSocketPath))
if ($needRemoteHome) {
    Write-Host "[INFO] Detecting remote home directory on $SshHost ..."
    $remoteHome = (& ssh $SshHost 'printf "%s" "$HOME"').Trim()
    if (-not $remoteHome) {
        throw "Failed to detect remote `$HOME on $SshHost. Pass -RemoteSocketPath explicitly or check SSH connectivity."
    }
    if (-not $RemoteSocketPath) {
        $RemoteSocketPath = "$remoteHome/.local/run/paper-bridge/helper.sock"
    }
    if (-not $RemoteZoteroReadSocketPath) {
        $RemoteZoteroReadSocketPath = "$remoteHome/.local/run/paper-bridge/zotero-read.sock"
    }
}

$sshArgs = @(
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=no"
)

$socketsToClean = @()
if ($UseTcpFallback) {
    $sshArgs += @("-R", "127.0.0.1:${HelperPort}:127.0.0.1:${HelperPort}")
} else {
    $socketsToClean += $RemoteSocketPath
    $sshArgs += @("-R", "${RemoteSocketPath}:127.0.0.1:${HelperPort}")
}

if (-not $NoZoteroReadBack) {
    if ($UseTcpZotero23119) {
        $sshArgs += @("-R", "127.0.0.1:23119:127.0.0.1:${ZoteroRoProxyPort}")
    } else {
        # Preferred: private chmod-700 Unix socket (avoids port 23119 conflicts & multi-user exposure)
        $socketsToClean += $RemoteZoteroReadSocketPath
        $sshArgs += @("-R", "${RemoteZoteroReadSocketPath}:127.0.0.1:${ZoteroRoProxyPort}")
    }
}

if ($socketsToClean.Count -gt 0) {
    $rmList = ($socketsToClean | ForEach-Object { "'$_'" }) -join " "
    Write-Host "[INFO] Cleaning stale remote sockets on $SshHost ..."
    & ssh $SshHost "mkdir -p ~/.local/run/paper-bridge && chmod 700 ~/.local/run/paper-bridge && rm -f $rmList"
}

$remoteCmd = "echo '[paper-bridge] Tunnel established. Draining offline queue...' && ~/.local/bin/paper-bridge drain || true; echo '[paper-bridge] Tunnel active. Press Ctrl+C to close.'; sleep infinity"
$sshArgs += @($SshHost, $remoteCmd)

Write-Host "[INFO] Opening reverse SSH tunnel to $SshHost ..."
try {
    & ssh @sshArgs
} finally {
    if ($helperProc -and -not $helperProc.HasExited) {
        Write-Host "[INFO] Stopping helper process (PID $($helperProc.Id)) ..."
        Stop-Process -Id $helperProc.Id -Force -ErrorAction SilentlyContinue
    }
}
