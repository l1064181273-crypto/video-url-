[CmdletBinding()]
param([string]$DataRoot = "")

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "lib/WindowsCommon.psm1") -Force

try {
    $ResolvedDataRoot = Get-LvtDataRoot -Configured $DataRoot
    $Context = Get-LvtInstalledContext -DataRoot $ResolvedDataRoot
    $ExitCode = Invoke-LvtLifecycle -Action "start" -Context $Context
    if ($ExitCode -eq 0) {
        Write-LvtStatus "INFO" "START_READY" "Local services are running"
        Write-LvtStatus "INFO" "START_HEALTH" "http://127.0.0.1:8765/health"
        Write-LvtStatus "INFO" "START_EXTENSION" (Join-Path $ResolvedDataRoot "extension")
    }
    elseif ($ExitCode -eq 1) {
        Write-LvtStatus "ERROR" "START_PREREQUISITE_MISSING" "Complete installation first"
    }
    else {
        Write-LvtStatus "ERROR" "START_UNSAFE" "Service ownership could not be verified"
    }
    exit $ExitCode
}
catch {
    Write-LvtStatus "ERROR" "START_UNSAFE" $_.Exception.Message
    exit 2
}
