[CmdletBinding()]
param([string]$DataRoot = "")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "lib/WindowsCommon.psm1") -Force

try {
    $ResolvedDataRoot = Get-LvtDataRoot -Configured $DataRoot
    $Context = Get-LvtInstalledContext -DataRoot $ResolvedDataRoot
    $ExitCode = Invoke-LvtLifecycle -Action "stop" -Context $Context
    if ($ExitCode -eq 0) {
        Write-LvtStatus "INFO" "STOP_COMPLETE" "Local services have stopped"
    }
    else {
        Write-LvtStatus "ERROR" "STOP_UNSAFE" "Service ownership could not be verified"
    }
    exit $ExitCode
}
catch {
    Write-LvtStatus "ERROR" "STOP_UNSAFE" $_.Exception.Message
    exit 2
}
