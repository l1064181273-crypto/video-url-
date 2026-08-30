Set-StrictMode -Version Latest

function Write-LvtStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Level,
        [Parameter(Mandatory = $true)][string]$Code,
        [Parameter(Mandatory = $true)][string]$Message
    )
    Write-Host ("[{0}] {1}: {2}" -f $Level, $Code, $Message)
}

function Test-LvtReparsePoint {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        return $false
    }
    $Item = Get-Item -LiteralPath $LiteralPath -Force
    return [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Assert-LvtPathWithoutReparsePoint {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    $FullPath = [IO.Path]::GetFullPath($LiteralPath)
    if ($FullPath.StartsWith("\\")) {
        throw "UNC paths are not allowed"
    }
    $Root = [IO.Path]::GetPathRoot($FullPath)
    $Current = $Root
    $Remainder = $FullPath.Substring($Root.Length)
    foreach ($Part in $Remainder.Split([IO.Path]::DirectorySeparatorChar)) {
        if ([string]::IsNullOrEmpty($Part)) {
            continue
        }
        $Current = Join-Path $Current $Part
        if (Test-LvtReparsePoint -LiteralPath $Current) {
            throw "Path contains a reparse point"
        }
        if (-not (Test-Path -LiteralPath $Current)) {
            break
        }
    }
    return $FullPath
}

function Get-LvtDataRoot {
    param([string]$Configured = "")
    if (-not [string]::IsNullOrWhiteSpace($Configured)) {
        return Assert-LvtPathWithoutReparsePoint -LiteralPath $Configured
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        throw "LOCALAPPDATA is unavailable"
    }
    return Assert-LvtPathWithoutReparsePoint -LiteralPath (
        Join-Path $env:LOCALAPPDATA "LocalVideoTranscriber"
    )
}

function Get-LvtInstalledContext {
    param([Parameter(Mandatory = $true)][string]$DataRoot)
    $StatePath = Join-Path $DataRoot "runtime/install-state.json"
    if (
        -not (Test-Path -LiteralPath $StatePath -PathType Leaf) -or
        (Test-LvtReparsePoint -LiteralPath $StatePath)
    ) {
        throw "Install state is unavailable"
    }
    $State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (
        $State.schema_version -ne 1 -or
        $State.core.verified -ne $true -or
        $State.core.activated -ne $true -or
        $State.core.release -notmatch "^app/releases/[0-9]+[.][0-9]+[.][0-9]+$"
    ) {
        throw "Installed release is not durably activated"
    }
    $RelativeRelease = $State.core.release.Replace("/", [IO.Path]::DirectorySeparatorChar)
    $ReleaseRoot = Assert-LvtPathWithoutReparsePoint -LiteralPath (
        Join-Path $DataRoot $RelativeRelease
    )
    $ExpectedRoot = [IO.Path]::GetFullPath((Join-Path $DataRoot "app/releases"))
    if (-not $ReleaseRoot.StartsWith($ExpectedRoot + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Installed release escapes the application root"
    }
    $VersionPath = Join-Path $ReleaseRoot "VERSION"
    $PythonPath = Join-Path $ReleaseRoot ".venv/Scripts/python.exe"
    if (
        -not (Test-Path -LiteralPath $VersionPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $PythonPath -PathType Leaf) -or
        (Test-LvtReparsePoint -LiteralPath $VersionPath) -or
        (Test-LvtReparsePoint -LiteralPath $PythonPath)
    ) {
        throw "Installed release is incomplete"
    }
    $Version = (Get-Content -LiteralPath $VersionPath -Raw -Encoding UTF8).Trim()
    if ($State.core.version -ne $Version) {
        throw "Installed release version is inconsistent"
    }
    return [PSCustomObject]@{
        DataRoot = $DataRoot
        ReleaseRoot = $ReleaseRoot
        Python = $PythonPath
    }
}

function Invoke-LvtLifecycle {
    param(
        [Parameter(Mandatory = $true)][string]$Action,
        [Parameter(Mandatory = $true)]$Context
    )
    $Tool = Join-Path $Context.ReleaseRoot "packaging/tools/windows_lifecycle.py"
    if (
        -not (Test-Path -LiteralPath $Tool -PathType Leaf) -or
        (Test-LvtReparsePoint -LiteralPath $Tool)
    ) {
        throw "Windows lifecycle tool is unavailable"
    }
    $Output = & $Context.Python $Tool $Action `
        --data-root $Context.DataRoot `
        --release-root $Context.ReleaseRoot
    $ExitCode = $LASTEXITCODE
    if ($null -ne $Output) {
        $Output | Write-Output
    }
    return $ExitCode
}

Export-ModuleMember -Function `
    Assert-LvtPathWithoutReparsePoint, `
    Get-LvtDataRoot, `
    Get-LvtInstalledContext, `
    Invoke-LvtLifecycle, `
    Test-LvtReparsePoint, `
    Write-LvtStatus
