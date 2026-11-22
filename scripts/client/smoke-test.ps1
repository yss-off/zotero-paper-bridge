<#
.SYNOPSIS
  Live smoke test for local Zotero 7 Read API and Connector Write API on Windows.
#>
param(
    [switch]$WriteTest,
    [switch]$TestCrossSessionAttach,
    [string]$Collection = "Agent-Inbox",
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$RootDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$env:PYTHONPATH = "$RootDir"

$cliArgs = @("-m", "paper_bridge.client.cli", "smoke-test")
if ($WriteTest) {
    $cliArgs += @("--write-test", "--collection", $Collection)
}
if ($TestCrossSessionAttach) {
    $cliArgs += @("--test-cross-session-attach")
}

& $PythonExe @cliArgs
