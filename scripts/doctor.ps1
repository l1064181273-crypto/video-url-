[CmdletBinding()]
param(
    [string]$DataRoot = "",
    [ValidateSet("dependencies", "installed-prerequisites", "runtime-full")]
    [string]$Phase = "installed-prerequisites",
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Import-Module (Join-Path $PSScriptRoot "lib/WindowsCommon.psm1") -Force

try {
    $ResolvedDataRoot = Get-LvtDataRoot -Configured $DataRoot
    $Context = Get-LvtInstalledContext -DataRoot $ResolvedDataRoot
    $Verifier = Join-Path $Context.ReleaseRoot "packaging/tools/verify_install.py"
    $Arguments = @(
        $Verifier,
        "--phase",
        $Phase,
        "--target",
        "windows-x64",
        "--data-root",
        $Context.DataRoot,
        "--release-root",
        $Context.ReleaseRoot
    )
    if ($Json) {
        $Arguments += "--json"
    }
    & $Context.Python @Arguments
    exit $LASTEXITCODE
}
catch {
    if ($Json) {
        @{
            schema_version = 1
            phase = $Phase
            status = "failed"
            exit_code = 2
            checks = @(
                @{
                    id = "windows_doctor"
                    status = "unsafe"
                    code = "WINDOWS_DOCTOR_FAILED"
                    message = $_.Exception.Message
                    suggestion = "Repair or reinstall Local Video Transcriber"
                }
            )
        } | ConvertTo-Json -Depth 4 -Compress
    }
    else {
        Write-LvtStatus "ERROR" "WINDOWS_DOCTOR_FAILED" $_.Exception.Message
    }
    exit 2
}
