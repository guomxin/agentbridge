[CmdletBinding()]
param(
    [ValidateSet("Targeted", "Full")]
    [string]$Mode = "Targeted",
    [string[]]$PythonTests = @(),
    [switch]$OpenClaw,
    [switch]$PackCheck,
    [switch]$McpApp,
    [switch]$SkipOpenClaw,
    [string]$VenvPath = "",
    [string]$BootstrapPython = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$npmCache = Join-Path $repoRoot ".npm-cache"
$stopwatch = [Diagnostics.Stopwatch]::StartNew()
$dependenciesUpdated = $false

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "$Label failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

function Invoke-NpmExternal {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    New-Item -ItemType Directory -Force -Path $npmCache | Out-Null
    $hadCache = Test-Path Env:NPM_CONFIG_CACHE
    $previousCache = $env:NPM_CONFIG_CACHE
    try {
        $env:NPM_CONFIG_CACHE = $npmCache
        Invoke-External -FilePath $FilePath -Arguments $Arguments -Label $Label -WorkingDirectory $WorkingDirectory
    }
    finally {
        if ($hadCache) {
            $env:NPM_CONFIG_CACHE = $previousCache
        }
        else {
            Remove-Item Env:NPM_CONFIG_CACHE -ErrorAction SilentlyContinue
        }
    }
}

function Resolve-BootstrapPython {
    if ($BootstrapPython) {
        if (-not (Test-Path -LiteralPath $BootstrapPython -PathType Leaf)) {
            throw "Bootstrap Python does not exist: $BootstrapPython"
        }
        return [pscustomobject]@{ FilePath = (Resolve-Path $BootstrapPython).Path; Prefix = @() }
    }

    if ($env:AGENTBRIDGE_TEST_PYTHON) {
        if (-not (Test-Path -LiteralPath $env:AGENTBRIDGE_TEST_PYTHON -PathType Leaf)) {
            throw "AGENTBRIDGE_TEST_PYTHON does not exist"
        }
        return [pscustomobject]@{
            FilePath = (Resolve-Path $env:AGENTBRIDGE_TEST_PYTHON).Path
            Prefix = @()
        }
    }

    $codexPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    if (Test-Path -LiteralPath $codexPython -PathType Leaf) {
        return [pscustomobject]@{ FilePath = $codexPython; Prefix = @() }
    }

    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        & $launcher.Source -3.12 -c "import sys; print(sys.executable)" *> $null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ FilePath = $launcher.Source; Prefix = @("-3.12") }
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) {
        $version = & $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.12") {
            return [pscustomobject]@{ FilePath = $python.Source; Prefix = @() }
        }
    }

    throw "Python 3.12+ was not found. Set AGENTBRIDGE_TEST_PYTHON or pass -BootstrapPython."
}

if (-not $VenvPath) {
    $cacheRoot = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:USERPROFILE }
    $VenvPath = Join-Path $cacheRoot "AgentBridge\test-venv-py312"
}
$VenvPath = [IO.Path]::GetFullPath($VenvPath)
$venvPython = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    $bootstrap = Resolve-BootstrapPython
    New-Item -ItemType Directory -Force -Path (Split-Path $VenvPath -Parent) | Out-Null
    $venvArguments = @($bootstrap.Prefix) + @("-m", "venv", $VenvPath)
    Invoke-External -FilePath $bootstrap.FilePath -Arguments $venvArguments -Label "Create persistent test environment" -WorkingDirectory $repoRoot
}

$pythonVersion = (& $venvPython -c "import sys; print(sys.version.split()[0])").Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Persistent test environment is not executable"
}
$projectHash = (Get-FileHash -LiteralPath (Join-Path $repoRoot "pyproject.toml") -Algorithm SHA256).Hash
$newline = [Environment]::NewLine
$dependencyStamp = "schema=3" + $newline + "python=$pythonVersion" + $newline + "pyproject=$projectHash" + $newline
$stampPath = Join-Path $VenvPath ".agentbridge-dependencies"
$currentStamp = if (Test-Path -LiteralPath $stampPath) {
    [IO.File]::ReadAllText($stampPath)
} else {
    ""
}

if ($currentStamp -ne $dependencyStamp) {
    Invoke-External -FilePath $venvPython -Arguments @(
        "-m", "pip", "install", "--disable-pip-version-check",
        "--no-build-isolation",
        "-e", "${repoRoot}[database-analysis]", "pytest", "pytest-xdist", "setuptools>=77"
    ) -Label "Install validation dependencies" -WorkingDirectory $repoRoot
    [IO.File]::WriteAllText($stampPath, $dependencyStamp, [Text.UTF8Encoding]::new($false))
    $dependenciesUpdated = $true
}

$runOpenClaw = ($Mode -eq "Full" -and -not $SkipOpenClaw) -or $OpenClaw
if ($Mode -eq "Targeted" -and $PythonTests.Count -eq 0 -and -not $runOpenClaw -and -not $McpApp) {
    throw "Targeted mode requires -PythonTests, -OpenClaw, or -McpApp."
}

if ($Mode -eq "Full") {
    if ($SkipOpenClaw) { throw "Full candidate validation cannot skip OpenClaw; use Targeted for partial checks." }
    $fullArguments = @("scripts/validation_plan.py", "full", "--root", $repoRoot)
    if ($McpApp) { $fullArguments += "--mcp-app" }
    Invoke-NpmExternal -FilePath $venvPython -Arguments $fullArguments -Label "Shared Full validation with stage evidence" -WorkingDirectory $repoRoot
}
elseif ($PythonTests.Count -gt 0) {
    $resolvedTests = @()
    foreach ($testPath in $PythonTests) {
        $candidate = if ([IO.Path]::IsPathRooted($testPath)) {
            $testPath
        } else {
            Join-Path $repoRoot $testPath
        }
        $resolved = (Resolve-Path -LiteralPath $candidate).Path
        if (-not $resolved.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Test path is outside the repository: $testPath"
        }
        $resolvedTests += $resolved
    }
    Invoke-External -FilePath $venvPython -Arguments (@("-m", "pytest", "-q") + $resolvedTests) -Label "Python targeted tests" -WorkingDirectory $repoRoot
}

if ($runOpenClaw -or $McpApp) {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        $npm = Get-Command npm -ErrorAction Stop
    }
}

if ($runOpenClaw -and $Mode -ne "Full") {
    $pluginRoot = Join-Path $repoRoot "integrations\openclaw-agentbridge"
    Invoke-NpmExternal -FilePath $npm.Source -Arguments @("test") -Label "OpenClaw plugin tests" -WorkingDirectory $pluginRoot
}

if ($runOpenClaw -and $Mode -ne "Full" -and $PackCheck) {
    $pluginRoot = Join-Path $repoRoot "integrations\openclaw-agentbridge"
    Invoke-NpmExternal -FilePath $npm.Source -Arguments @("run", "pack:check") -Label "OpenClaw package manifest check" -WorkingDirectory $pluginRoot
}

if ($McpApp -and $Mode -ne "Full") {
    $appRoot = Join-Path $repoRoot "integrations\mcp-app"
    Invoke-NpmExternal -FilePath $npm.Source -Arguments @("run", "check") -Label "MCP App checks" -WorkingDirectory $appRoot
    Invoke-NpmExternal -FilePath $npm.Source -Arguments @("run", "build") -Label "MCP App build" -WorkingDirectory $appRoot
}

$stopwatch.Stop()
[ordered]@{
    status = "succeeded"
    mode = $Mode
    python = $venvPython
    pythonVersion = $pythonVersion
    dependenciesUpdated = $dependenciesUpdated
    pythonTestCount = $PythonTests.Count
    pythonWorkers = $(if ($Mode -eq "Full") { 4 } else { 1 })
    openClaw = [bool]$runOpenClaw
    packCheck = [bool]($runOpenClaw -and ($Mode -eq "Full" -or $PackCheck))
    mcpApp = [bool]$McpApp
    elapsedSeconds = [math]::Round($stopwatch.Elapsed.TotalSeconds, 2)
} | ConvertTo-Json -Compress
