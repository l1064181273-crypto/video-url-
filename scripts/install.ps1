[CmdletBinding()]
param(
    [ValidateSet("all", "staging-core", "dependencies", "publish")]
    [string]$Phase = "all",
    [string]$DataRoot = "",
    [switch]$SkipModels
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$env:PYTHONUTF8 = "1"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$PythonUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/20260623/cpython-3.11.15%2B20260623-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
$PythonSha256 = "6589ca6d63f520bec4096d62b3ab91da3d0a80b16b594c99a6b677e335814683"
$PythonSize = 25682043
$SourceRoot = Split-Path -Parent $PSScriptRoot
$BootstrapRoot = $null

function Write-LvtStatus {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Level,
        [Parameter(Mandatory = $true)]
        [string]$Code,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )
    Write-Host ("[{0}] {1}: {2}" -f $Level, $Code, $Message)
}

function Test-ReparsePoint {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        return $false
    }
    $Item = Get-Item -LiteralPath $LiteralPath -Force
    return [bool]($Item.Attributes -band [IO.FileAttributes]::ReparsePoint)
}

function Protect-LvtDataRoot {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)
    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        New-Item -ItemType Directory -Path $LiteralPath | Out-Null
    }
    if (
        -not (Test-Path -LiteralPath $LiteralPath -PathType Container) -or
        (Test-ReparsePoint -LiteralPath $LiteralPath)
    ) {
        throw "Data root is unsafe"
    }
    $UserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $Icacls = Join-Path $env:SystemRoot "System32/icacls.exe"
    $UserGrant = "*${UserSid}:(OI)(CI)F"
    $SystemGrant = "*S-1-5-18:(OI)(CI)F"
    & $Icacls $LiteralPath "/inheritance:r" "/grant:r" $UserGrant $SystemGrant | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Data root ACL could not be secured"
    }
}

function Compare-LvtVersion {
    param(
        [Parameter(Mandatory = $true)][string]$Left,
        [Parameter(Mandatory = $true)][string]$Right
    )
    if (
        $Left -notmatch "^[0-9]+[.][0-9]+[.][0-9]+$" -or
        $Right -notmatch "^[0-9]+[.][0-9]+[.][0-9]+$"
    ) {
        throw "Version is invalid"
    }
    $LeftParts = @($Left.Split(".") | ForEach-Object { [int]$_ })
    $RightParts = @($Right.Split(".") | ForEach-Object { [int]$_ })
    for ($Index = 0; $Index -lt 3; $Index++) {
        if ($LeftParts[$Index] -lt $RightParts[$Index]) {
            return -1
        }
        if ($LeftParts[$Index] -gt $RightParts[$Index]) {
            return 1
        }
    }
    return 0
}

function Assert-VerifiedFile {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][long]$ExpectedSize
    )
    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        throw "Verified file is unavailable"
    }
    if (Test-ReparsePoint -LiteralPath $LiteralPath) {
        throw "Verified file is a reparse point"
    }
    $Item = Get-Item -LiteralPath $LiteralPath -Force
    if ($Item.Length -ne $ExpectedSize) {
        throw "Verified file size mismatch"
    }
    $ActualSha256 = (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualSha256 -ne $ExpectedSha256) {
        throw "Verified file digest mismatch"
    }
}

function Get-VerifiedDownload {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [Parameter(Mandatory = $true)][string]$ExpectedSha256,
        [Parameter(Mandatory = $true)][long]$ExpectedSize
    )
    $CurrentUri = [Uri]$Uri
    for ($RedirectCount = 0; $RedirectCount -le 10; $RedirectCount++) {
        if ($CurrentUri.Scheme -ne "https" -or -not [string]::IsNullOrEmpty($CurrentUri.UserInfo)) {
            throw "Download URI violates trust policy"
        }
        $Request = [Net.HttpWebRequest]::Create($CurrentUri)
        $Request.AllowAutoRedirect = $false
        $Request.Method = "GET"
        $Request.Timeout = 60000
        $Request.ReadWriteTimeout = 60000
        $Request.UserAgent = "LocalVideoTranscriber-Installer/1"
        $Response = $null
        try {
            $Response = [Net.HttpWebResponse]$Request.GetResponse()
            $StatusCode = [int]$Response.StatusCode
            if ($StatusCode -ge 300 -and $StatusCode -lt 400) {
                $Location = $Response.Headers["Location"]
                if ([string]::IsNullOrWhiteSpace($Location)) {
                    throw "Download redirect is missing a destination"
                }
                $CurrentUri = [Uri]::new($CurrentUri, $Location)
                continue
            }
            if ($StatusCode -ne 200) {
                throw "Download returned an unexpected status"
            }
            $Stream = $Response.GetResponseStream()
            $Output = [IO.File]::Open(
                $Destination,
                [IO.FileMode]::CreateNew,
                [IO.FileAccess]::Write,
                [IO.FileShare]::None
            )
            try {
                $Buffer = New-Object byte[] (1024 * 1024)
                [long]$Written = 0
                while (($Count = $Stream.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
                    $Written += $Count
                    if ($Written -gt $ExpectedSize) {
                        throw "Download exceeds pinned size"
                    }
                    $Output.Write($Buffer, 0, $Count)
                }
                $Output.Flush($true)
            }
            finally {
                $Output.Dispose()
                $Stream.Dispose()
            }
            Assert-VerifiedFile `
                -LiteralPath $Destination `
                -ExpectedSha256 $ExpectedSha256 `
                -ExpectedSize $ExpectedSize
            return
        }
        finally {
            if ($null -ne $Response) {
                $Response.Dispose()
            }
        }
    }
    throw "Download exceeded redirect limit"
}

function Assert-SafeTarEntries {
    param([Parameter(Mandatory = $true)][string]$Archive)
    $Tar = Get-Command "tar.exe" -ErrorAction Stop
    $Entries = & $Tar.Source -tzf $Archive
    if ($LASTEXITCODE -ne 0) {
        throw "Python archive cannot be listed"
    }
    foreach ($Entry in $Entries) {
        $Normalized = $Entry.TrimEnd("/")
        if ([string]::IsNullOrWhiteSpace($Normalized)) {
            continue
        }
        if (
            $Normalized.StartsWith("/") -or
            $Normalized.StartsWith("\") -or
            $Normalized.Contains("\") -or
            $Normalized.Contains(":")
        ) {
            throw "Python archive contains an unsafe path"
        }
        foreach ($Part in $Normalized.Split("/")) {
            if ([string]::IsNullOrEmpty($Part) -or $Part -eq "." -or $Part -eq "..") {
                throw "Python archive contains an unsafe path"
            }
        }
    }
}

function New-BootstrapPython {
    $TempParent = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (Test-ReparsePoint -LiteralPath $TempParent) {
        throw "Temporary directory is a reparse point"
    }
    $script:BootstrapRoot = Join-Path $TempParent ("lvt-python-bootstrap." + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:BootstrapRoot | Out-Null
    $Archive = Join-Path $script:BootstrapRoot "python-runtime.tar.gz"
    if (
        $env:LVT_TEST_ROOT -and
        $env:LVT_TEST_FORCE_PYTHON_BOOTSTRAP -eq "1" -and
        $env:LVT_TEST_BOOTSTRAP_PYTHON_ARCHIVE
    ) {
        Copy-Item -LiteralPath $env:LVT_TEST_BOOTSTRAP_PYTHON_ARCHIVE -Destination $Archive
        Assert-VerifiedFile `
            -LiteralPath $Archive `
            -ExpectedSha256 $env:LVT_TEST_BOOTSTRAP_PYTHON_SHA256 `
            -ExpectedSize ([long]$env:LVT_TEST_BOOTSTRAP_PYTHON_SIZE)
    }
    else {
        Get-VerifiedDownload `
            -Uri $PythonUrl `
            -Destination $Archive `
            -ExpectedSha256 $PythonSha256 `
            -ExpectedSize $PythonSize
    }
    Assert-SafeTarEntries -Archive $Archive
    $ExtractRoot = Join-Path $script:BootstrapRoot "extract"
    New-Item -ItemType Directory -Path $ExtractRoot | Out-Null
    $Tar = Get-Command "tar.exe" -ErrorAction Stop
    & $Tar.Source -xzf $Archive -C $ExtractRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Python archive extraction failed"
    }
    $PythonRoot = Join-Path $ExtractRoot "python"
    $Python = Join-Path $PythonRoot "python.exe"
    if (
        -not (Test-Path -LiteralPath $PythonRoot -PathType Container) -or
        -not (Test-Path -LiteralPath $Python -PathType Leaf) -or
        (Test-ReparsePoint -LiteralPath $PythonRoot) -or
        (Test-ReparsePoint -LiteralPath $Python)
    ) {
        throw "Python archive layout is invalid"
    }
    return $PythonRoot
}

try {
    if ([Runtime.InteropServices.RuntimeInformation]::OSArchitecture -ne "X64") {
        throw "Windows x64 is required"
    }
    if ([string]::IsNullOrWhiteSpace($DataRoot)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw "LOCALAPPDATA is unavailable"
        }
        $DataRoot = Join-Path $env:LOCALAPPDATA "LocalVideoTranscriber"
    }
    $DataRoot = [IO.Path]::GetFullPath($DataRoot)
    if ($DataRoot.StartsWith("\\") -or (Test-ReparsePoint -LiteralPath $DataRoot)) {
        throw "Data root is unsafe"
    }
    Protect-LvtDataRoot -LiteralPath $DataRoot
    $VersionPath = Join-Path $SourceRoot "VERSION"
    $Version = (Get-Content -LiteralPath $VersionPath -Raw -Encoding UTF8).Trim()
    if ($Version -notmatch "^[0-9]+[.][0-9]+[.][0-9]+$") {
        throw "Release version is invalid"
    }
    $ExistingStatePath = Join-Path $DataRoot "runtime/install-state.json"
    if (
        (Test-Path -LiteralPath $ExistingStatePath -PathType Leaf) -and
        -not (Test-ReparsePoint -LiteralPath $ExistingStatePath)
    ) {
        $ExistingState = Get-Content -LiteralPath $ExistingStatePath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if (
            $null -ne $ExistingState.core -and
            -not [string]::IsNullOrWhiteSpace($ExistingState.core.version) -and
            (Compare-LvtVersion -Left $Version -Right $ExistingState.core.version) -lt 0
        ) {
            throw "WINDOWS_DOWNGRADE_REFUSED"
        }
    }
    $InstallTool = Join-Path $SourceRoot "packaging/tools/install.py"
    if (
        -not (Test-Path -LiteralPath $InstallTool -PathType Leaf) -or
        (Test-ReparsePoint -LiteralPath $InstallTool)
    ) {
        throw "Install tool is unavailable"
    }
    $InstalledPython = Join-Path $DataRoot "app/tools/python/python.exe"
    $BootstrapPythonRoot = $null
    if (
        (Test-Path -LiteralPath $InstalledPython -PathType Leaf) -and
        -not (Test-ReparsePoint -LiteralPath $InstalledPython)
    ) {
        $Python = $InstalledPython
    }
    else {
        Write-LvtStatus "INFO" "PYTHON_BOOTSTRAP_START" "Preparing pinned Python runtime"
        $BootstrapPythonRoot = New-BootstrapPython
        $Python = Join-Path $BootstrapPythonRoot "python.exe"
        Write-LvtStatus "INFO" "PYTHON_BOOTSTRAP_READY" "Pinned Python runtime verified"
    }
    if ($Phase -in @("all", "staging-core", "dependencies")) {
        $Arguments = @(
            $InstallTool,
            "--phase",
            "staging-core",
            "--data-root",
            $DataRoot
        )
        if ($null -ne $BootstrapPythonRoot) {
            $Arguments += @("--bootstrap-python-root", $BootstrapPythonRoot)
        }
        & $Python @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Core installer failed"
        }
        Write-LvtStatus "INFO" "WINDOWS_STAGING_READY" "Core candidate is installed and verified"
        if ($Phase -eq "staging-core") {
            exit 0
        }
    }

    $Python = $InstalledPython
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Installed Python is unavailable"
    }
    $Candidate = Join-Path $DataRoot ("app/releases/" + $Version)

    if ($Phase -in @("all", "dependencies")) {
        $ProvisionTool = Join-Path $Candidate "packaging/tools/provision.py"
        $ProvisionArguments = @(
            $ProvisionTool,
            "--phase",
            "dependencies",
            "--data-root",
            $DataRoot,
            "--release-root",
            $Candidate
        )
        if ($SkipModels) {
            $ProvisionArguments += "--skip-models"
        }
        & $Python @ProvisionArguments
        $ProvisionExitCode = $LASTEXITCODE
        if ($ProvisionExitCode -ne 0) {
            if ($SkipModels -and $ProvisionExitCode -eq 1) {
                Write-LvtStatus "WARN" "WINDOWS_DEPENDENCIES_INCOMPLETE" "Model installation was skipped"
                exit 1
            }
            throw "Dependency provisioning failed"
        }
        Write-LvtStatus "INFO" "WINDOWS_DEPENDENCIES_READY" "Runtime dependencies are verified"
        if ($Phase -eq "dependencies") {
            exit 0
        }
    }

    if ($SkipModels) {
        throw "A skipped model installation cannot be published"
    }
    $PublishTool = Join-Path $Candidate "packaging/tools/windows_publish_install.py"
    & $Python $PublishTool --data-root $DataRoot --release-root $Candidate
    if ($LASTEXITCODE -ne 0) {
        throw "Release publication failed"
    }
    Write-LvtStatus "INFO" "WINDOWS_READY" "Release is activated and local services are running"
    exit 0
}
catch {
    Write-LvtStatus "ERROR" "WINDOWS_INSTALL_FAILED" $_.Exception.Message
    exit 2
}
finally {
    if (
        $null -ne $BootstrapRoot -and
        (Test-Path -LiteralPath $BootstrapRoot) -and
        $BootstrapRoot.StartsWith([IO.Path]::GetFullPath([IO.Path]::GetTempPath()))
    ) {
        Remove-Item -LiteralPath $BootstrapRoot -Recurse -Force
    }
}
