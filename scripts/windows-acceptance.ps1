[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Evidence = Join-Path $Root "windows-evidence"
$Python = Join-Path $Root "backend/.venv/Scripts/python.exe"
$DataRoot = Join-Path $env:RUNNER_TEMP "LocalVideoTranscriber-acceptance"
$ExtractRoot = Join-Path $env:RUNNER_TEMP "LocalVideoTranscriber-package"
$PackageRoot = $null
$Failed = $false

New-Item -ItemType Directory -Path $Evidence -Force | Out-Null

function Invoke-EvidenceCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Command
    )
    $OutputPath = Join-Path $Evidence ($Name + ".txt")
    $ExitPath = Join-Path $Evidence ($Name + ".exit-code")
    $global:LASTEXITCODE = 0
    try {
        & $Command *>&1 | Tee-Object -FilePath $OutputPath
        $Code = $LASTEXITCODE
        if ($null -eq $Code) {
            $Code = 0
        }
    }
    catch {
        $_ | Out-String | Add-Content -LiteralPath $OutputPath
        $Code = 1
    }
    Set-Content -LiteralPath $ExitPath -Value $Code -Encoding ASCII
    if ($Code -ne 0) {
        throw "$Name failed with exit code $Code"
    }
}

try {
    if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne "X64") {
        throw "Windows x64 runner is required"
    }
    Push-Location $Root
    try {
        Invoke-EvidenceCommand "ruff-packaging" {
            & $Python -m ruff check --config backend/pyproject.toml packaging/tools packaging/tests
        }
        Invoke-EvidenceCommand "pytest-windows" {
            $env:PYTHONPATH = "backend/src"
            & $Python -m pytest -o "addopts=" -q `
                packaging/tests/test_windows_install_layout.py `
                packaging/tests/test_windows_process.py `
                packaging/tests/test_windows_job.py `
                packaging/tests/test_windows_service.py `
                packaging/tests/test_windows_supervisor.py `
                packaging/tests/test_windows_lifecycle.py `
                packaging/tests/test_windows_publication.py `
                packaging/tests/test_windows_publish.py `
                packaging/tests/test_package_windows_release.py
        }
        Invoke-EvidenceCommand "pytest-backend-unit" {
            $env:PYTHONPATH = "backend/src"
            & $Python -m pytest -o "addopts=" -q backend/tests/unit
        }
        Invoke-EvidenceCommand "extension-lint" {
            npm --prefix extension run lint
        }
        Invoke-EvidenceCommand "extension-typecheck" {
            npm --prefix extension run typecheck
        }
        Invoke-EvidenceCommand "extension-unit" {
            npm --prefix extension test
        }
        Invoke-EvidenceCommand "extension-build" {
            npm --prefix extension run build
        }

        $First = Join-Path $env:RUNNER_TEMP "lvt-package-first"
        $Second = Join-Path $env:RUNNER_TEMP "lvt-package-second"
        New-Item -ItemType Directory -Path $First, $Second -Force | Out-Null
        Invoke-EvidenceCommand "package-first" {
            & $Python packaging/tools/package_windows_release.py --output-dir $First
        }
        Invoke-EvidenceCommand "package-second" {
            & $Python packaging/tools/package_windows_release.py --output-dir $Second
        }
        $ArchiveName = "LocalVideoTranscriber-0.1.1-windows-x64.zip"
        $FirstArchive = Join-Path $First $ArchiveName
        $SecondArchive = Join-Path $Second $ArchiveName
        if (
            (Get-FileHash -LiteralPath $FirstArchive -Algorithm SHA256).Hash -ne
            (Get-FileHash -LiteralPath $SecondArchive -Algorithm SHA256).Hash
        ) {
            throw "Windows package is not reproducible"
        }
        New-Item -ItemType Directory -Path (Join-Path $Root "dist-windows") -Force | Out-Null
        Copy-Item -LiteralPath $FirstArchive -Destination (Join-Path $Root "dist-windows")
        Copy-Item -LiteralPath ($FirstArchive + ".sha256") -Destination (
            Join-Path $Root "dist-windows"
        )

        Remove-Item -LiteralPath $ExtractRoot -Recurse -Force -ErrorAction SilentlyContinue
        Expand-Archive -LiteralPath $FirstArchive -DestinationPath $ExtractRoot
        $PackageRoot = Join-Path $ExtractRoot "LocalVideoTranscriber-0.1.1"
        Invoke-EvidenceCommand "native-staging-core" {
            & (Join-Path $PackageRoot "scripts/install.ps1") `
                -Phase staging-core `
                -DataRoot $DataRoot
        }

        if ($env:LVT_FULL_MODEL_INSTALL -eq "true") {
            Invoke-EvidenceCommand "native-dependencies" {
                & (Join-Path $PackageRoot "scripts/install.ps1") `
                    -Phase dependencies `
                    -DataRoot $DataRoot
            }
            Invoke-EvidenceCommand "native-asr-cpu" {
                & (Join-Path $DataRoot "app/releases/0.1.1/.venv/Scripts/python.exe") `
                    (Join-Path $Root "scripts/windows-asr-smoke.py") `
                    --model-directory (Join-Path $DataRoot "models/asr/faster-whisper-small")
            }
            Invoke-EvidenceCommand "native-publish" {
                & (Join-Path $PackageRoot "scripts/install.ps1") `
                    -Phase publish `
                    -DataRoot $DataRoot
            }
            Invoke-EvidenceCommand "native-runtime-doctor" {
                & (Join-Path $PackageRoot "scripts/doctor.ps1") `
                    -DataRoot $DataRoot `
                    -Phase runtime-full `
                    -Json
            }
            Invoke-EvidenceCommand "chrome-e2e" {
                $env:LVT_E2E_PYTHON = $Python
                npm --prefix extension run test:e2e
            }
        }
    }
    finally {
        Pop-Location
    }
}
catch {
    $Failed = $true
    $_ | Out-String | Set-Content -LiteralPath (
        Join-Path $Evidence "acceptance-error.txt"
    )
}
finally {
    $InstalledStatePath = Join-Path $DataRoot "runtime/install-state.json"
    $ShouldStop = $false
    if ($null -ne $PackageRoot -and (Test-Path -LiteralPath $InstalledStatePath)) {
        try {
            $InstalledState = Get-Content -LiteralPath $InstalledStatePath -Raw -Encoding UTF8 |
                ConvertFrom-Json
            $Activated = $InstalledState.core.PSObject.Properties["activated"]
            $ShouldStop = $null -ne $Activated -and $Activated.Value -eq $true
        }
        catch {
            $Failed = $true
        }
    }
    if ($ShouldStop) {
        & (Join-Path $PackageRoot "scripts/stop.ps1") -DataRoot $DataRoot *>&1 |
            Set-Content -LiteralPath (Join-Path $Evidence "final-stop.txt")
        Set-Content -LiteralPath (Join-Path $Evidence "final-stop.exit-code") `
            -Value $LASTEXITCODE `
            -Encoding ASCII
        if ($LASTEXITCODE -ne 0) {
            $Failed = $true
        }
    }
    Start-Sleep -Seconds 1
    $Listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in @(8765, 11435) } |
        Select-Object LocalAddress, LocalPort, OwningProcess
    ConvertTo-Json -InputObject @($Listeners) -Depth 3 | Set-Content -LiteralPath (
        Join-Path $Evidence "final-listeners.json"
    )
    if ($Listeners) {
        $Failed = $true
    }
    $Processes = @()
    try {
        $DataRootPattern = [Regex]::Escape($DataRoot)
        $Processes = @(
            Get-CimInstance Win32_Process -ErrorAction Stop |
                Where-Object {
                    $null -ne $_.CommandLine -and $_.CommandLine -match $DataRootPattern
                } |
                Select-Object ProcessId, Name
        )
    }
    catch {
        $Failed = $true
        $_ | Out-String | Set-Content -LiteralPath (
            Join-Path $Evidence "final-process-audit-error.txt"
        )
    }
    ConvertTo-Json -InputObject @($Processes) -Depth 3 | Set-Content -LiteralPath (
        Join-Path $Evidence "final-processes.json"
    )
    if ($Processes) {
        $Failed = $true
    }
    Remove-Item -LiteralPath $ExtractRoot -Recurse -Force -ErrorAction SilentlyContinue
    if (-not $Listeners -and -not $Processes) {
        Remove-Item -LiteralPath $DataRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

if ($Failed) {
    exit 1
}
exit 0
