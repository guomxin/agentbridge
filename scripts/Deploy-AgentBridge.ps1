#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$EnvironmentProfile = $env:AGENTBRIDGE_ENVIRONMENT_PROFILE,
    [string]$HostName = "10.10.50.213",
    [string]$SshUser = "root",
    [string]$IdentityFile = "",
    [string]$KnownHostsFile = "",
    [string]$RemoteRoot = "/home/guomao/agentbridge",
    [string]$ServiceName = "agentbridge",
    [string]$VenvPath = "",
    [switch]$SkipValidation,
    [switch]$SkipSmoke,
    [switch]$IncludeLoginReuseSmoke,
    [switch]$RestartOpenClaw,
    [switch]$ForceRestartOpenClaw,
    [switch]$InstallSystemDependencies,
    [switch]$AllowDirty,
    [switch]$ResumeCompletion,
    [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$validationScript = Join-Path $PSScriptRoot "Invoke-AgentBridgeValidation.ps1"
$smokeScript = Join-Path $PSScriptRoot "Test-AgentBridgeMcp.ps1"
$gatewayWarmupScript = Join-Path $PSScriptRoot "Test-OpenClawGatewayWarmup.ps1"
$gatewayLifecycleScript = Join-Path $PSScriptRoot "Restart-AgentBridgeOpenClawGateway.ps1"
$gatewayRuntimeScript = Join-Path $PSScriptRoot "Test-AgentBridgeOpenClawRuntime.ps1"
$gatewayWarmupPendingPath = Join-Path $env:LOCALAPPDATA "AgentBridge\openclaw-release-warmup.pending"

if ($HostName -notmatch '^[A-Za-z0-9.-]+$') {
    throw "HostName contains unsupported characters"
}
if ($SshUser -notmatch '^[A-Za-z0-9._-]+$') {
    throw "SshUser contains unsupported characters"
}
if ($ServiceName -notmatch '^[A-Za-z0-9_.@-]+$') {
    throw "ServiceName contains unsupported characters"
}
if ($RemoteRoot -notmatch '^/home/[A-Za-z0-9._/-]+$' -or $RemoteRoot.Contains("..")) {
    throw "RemoteRoot must be a fixed path below /home"
}
Import-Module (Join-Path $PSScriptRoot 'AgentBridgeEnvironment.psm1') -Force
$environmentConfig = Read-AgentBridgeEnvironment -Path $EnvironmentProfile -RepoRoot $repoRoot -HostName $HostName -RemoteRoot $RemoteRoot
$systemdUnit = $environmentConfig.Units["$ServiceName.service"].Path
$backupSystemdUnit = $environmentConfig.Units["$ServiceName-backup.service"].Path
$backupSystemdTimer = $environmentConfig.Units["$ServiceName-backup.timer"].Path
if (-not (Test-Path -LiteralPath $systemdUnit -PathType Leaf)) {
    throw "Selected environment systemd unit was not found: $systemdUnit"
}
if (-not (Test-Path -LiteralPath $backupSystemdUnit -PathType Leaf)) {
    throw "Selected environment backup systemd unit was not found: $backupSystemdUnit"
}
if (-not (Test-Path -LiteralPath $backupSystemdTimer -PathType Leaf)) {
    throw "Selected environment backup systemd timer was not found: $backupSystemdTimer"
}

if (-not $IdentityFile) {
    $IdentityFile = Join-Path $env:USERPROFILE ".ssh\id_ed25519_10_10_50_213"
}
if (-not $KnownHostsFile) {
    $KnownHostsFile = Join-Path $repoRoot "deploy\ssh\agentbridge_known_hosts"
}
if (-not $VenvPath) {
    $cacheRoot = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:USERPROFILE }
    $VenvPath = Join-Path $cacheRoot "AgentBridge\test-venv-py312"
}
$VenvPath = [IO.Path]::GetFullPath($VenvPath)
$venvPython = Join-Path $VenvPath "Scripts\python.exe"

$gitDir = if (Test-Path -LiteralPath (Join-Path $repoRoot ".gitrepo")) {
    Join-Path $repoRoot ".gitrepo"
} else {
    Join-Path $repoRoot ".git"
}
$gitArguments = @("--git-dir=$gitDir", "--work-tree=$repoRoot")
$commit = ((& git @gitArguments rev-parse --short=12 HEAD) | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{7,12}$') {
    throw "Unable to resolve the repository commit"
}
$trackedChanges = ((& git @gitArguments status --porcelain --untracked-files=no) | Out-String).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the repository state"
}
$isDirty = [bool]$trackedChanges
if ($isDirty -and -not $AllowDirty -and -not $PlanOnly) {
    throw "Tracked files are modified. Commit them first or use -AllowDirty for a development-only deployment."
}
$releaseId = if ($isDirty) { "$commit-dirty" } else { $commit }

Import-Module (Join-Path $PSScriptRoot "AgentBridgeReleasePreflight.psm1") -Force
$releasePreflight = Get-AgentBridgeReleasePreflight -HostName $HostName -SshUser $SshUser `
    -RemoteRoot $RemoteRoot -IdentityFile $IdentityFile -KnownHostsFile $KnownHostsFile `
    -CandidateRelease $commit -PolicyPath (Join-Path $repoRoot 'deploy/release-policy.json') -ResumeAcceptance:$ResumeCompletion

Import-Module (Join-Path $PSScriptRoot "AgentBridgeOpenClawRestartPolicy.psm1") -Force
# RestartOpenClaw is retained as a compatible request for conditional restart.
# The input baseline, not a carried-over command-line flag, decides necessity.
$restartPlan = Get-AgentBridgeOpenClawRestartPlan -RepoRoot $repoRoot -Force:$ForceRestartOpenClaw
$RestartOpenClaw = [bool]$restartPlan.required
$gatewayRestartPerformed = $false
$plan = [ordered]@{
    status = "planned"
    releaseId = $releaseId
    resumeCompletion = [bool]$ResumeCompletion
    releasePreflight = $releasePreflight
    target = "$SshUser@$HostName"
    remoteRoot = $RemoteRoot
    validation = -not $SkipValidation
    smoke = -not $SkipSmoke
    loginReuseSmoke = [bool]$IncludeLoginReuseSmoke
    restartOpenClaw = [bool]$RestartOpenClaw
    openClawRestartReason = $restartPlan.reason
    openClawChangedInputs = @($restartPlan.changedInputs)
    openClawGuardrails = [bool]$RestartOpenClaw
    openClawWarmup = [bool]$RestartOpenClaw -or (Test-Path -LiteralPath $gatewayWarmupPendingPath)
    installSystemDependencies = [bool]$InstallSystemDependencies
    systemdUnits = @($systemdUnit, $backupSystemdUnit, $backupSystemdTimer)
    environmentProfile = $environmentConfig.Path
    unitHashes = @($environmentConfig.Units.Values | ForEach-Object { $_.Hash })
}
if ($PlanOnly) {
    $plan | ConvertTo-Json -Compress
    exit 0
}

if ($isDirty) {
    throw "Dirty deployment is no longer supported: commit the candidate in an isolated checkout. -AllowDirty cannot bypass artifact validation."
}

if (-not (Test-Path -LiteralPath $IdentityFile -PathType Leaf)) {
    throw "SSH identity file was not found"
}
if (-not (Test-Path -LiteralPath $KnownHostsFile -PathType Leaf)) {
    throw "SSH known-hosts file was not found"
}

if (-not $SkipValidation) {
    $validationParameters = @{
        Mode = "Full"
        VenvPath = $VenvPath
    }
    & $validationScript @validationParameters
    if ($LASTEXITCODE -ne 0) { throw "Candidate validation failed" }
}

if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
    throw "Persistent validation environment is missing; run validation first"
}

$artifactRaw = & $venvPython (Join-Path $repoRoot "scripts/agentbridge_artifact.py") verify --root $repoRoot
if ($LASTEXITCODE -ne 0) { throw "A matching validated candidate is required before deployment" }
$artifact = ($artifactRaw | Out-String) | ConvertFrom-Json
$wheel = Get-Item -LiteralPath $artifact.wheel
$releaseDirectory = Split-Path -Parent $artifact.manifest
$artifactBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes($artifact.manifest))
$receiptBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $repoRoot "output/release-validation/full.json")))
if ($wheel.Name -notmatch '^[A-Za-z0-9_.+-]+\.whl$') {
    throw "The AgentBridge wheel filename is unsafe"
}

$ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
if (-not $ssh) { $ssh = Get-Command ssh -ErrorAction Stop }
$scp = Get-Command scp.exe -ErrorAction SilentlyContinue
if (-not $scp) { $scp = Get-Command scp -ErrorAction Stop }
$connectionArguments = @(
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "UserKnownHostsFile=$((Resolve-Path $KnownHostsFile).Path)",
    "-i", (Resolve-Path $IdentityFile).Path
)
$target = "$SshUser@$HostName"
$remoteWheel = "/tmp/$releaseId-$([guid]::NewGuid().ToString('N'))-$($wheel.Name)"
$remoteDestination = $target + ":" + $remoteWheel
$systemdUnitBase64 = [Convert]::ToBase64String($environmentConfig.Units["$ServiceName.service"].Bytes)
$backupSystemdUnitBase64 = [Convert]::ToBase64String($environmentConfig.Units["$ServiceName-backup.service"].Bytes)
$backupSystemdTimerBase64 = [Convert]::ToBase64String($environmentConfig.Units["$ServiceName-backup.timer"].Bytes)
$releasePolicy = Get-Content (Join-Path $repoRoot "deploy/release-policy.json") -Raw | ConvertFrom-Json
$releaseRunnerBase64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot "agentbridge_release.py")))
$releaseConfig = @{
    root = $RemoteRoot; releaseId = $releaseId; service = $ServiceName; host = $HostName
    wheel = $remoteWheel; wheelName = $wheel.Name; sha256 = $artifact.sha256
    artifact = $artifactBase64; validation = $receiptBase64; policy = $releasePolicy
    units = @{
        "$ServiceName.service" = $systemdUnitBase64
        "$ServiceName-backup.service" = $backupSystemdUnitBase64
        "$ServiceName-backup.timer" = $backupSystemdTimerBase64
    }
} | ConvertTo-Json -Compress -Depth 10
$releaseConfigBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($releaseConfig))

if (-not $ResumeCompletion) {
& $scp.Source @connectionArguments $wheel.FullName $remoteDestination
if ($LASTEXITCODE -ne 0) {
    throw "Uploading the AgentBridge wheel failed"
}

$remoteTemplate = @(
    'set -euo pipefail',
    'wheel=''__REMOTE_WHEEL__''',
    'root=''__REMOTE_ROOT__''',
    'release_id=''__RELEASE_ID__''',
    'service=''__SERVICE_NAME__''',
    'python="$root/venv/bin/python"',
    'unit_tmp_dir="$(mktemp -d "/tmp/agentbridge-systemd-$release_id.XXXXXX")"',
    'install_system_dependencies=''__INSTALL_SYSTEM_DEPENDENCIES__''',
    'trap ''rm -f -- "$wheel"; rm -rf -- "$unit_tmp_dir"'' EXIT',
    'if [ "$install_system_dependencies" = "1" ]; then DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xvfb x11vnc novnc websockify xauth; fi',
    'for command in Xvfb x11vnc websockify xauth; do command -v "$command" >/dev/null || { printf ''%s is required; deploy once with -InstallSystemDependencies\n'' "$command" >&2; exit 1; }; done',
    'test -d /usr/share/novnc || { printf ''noVNC web root is required; deploy once with -InstallSystemDependencies\n'' >&2; exit 1; }',
    'install -d -m 0700 "$unit_tmp_dir"',
    'printf ''%s'' ''__RELEASE_RUNNER__'' | base64 --decode > "$unit_tmp_dir/release.py"',
    'printf ''%s'' ''__RELEASE_CONFIG__'' | base64 --decode > "$unit_tmp_dir/config.json"',
    '"$python" -I "$unit_tmp_dir/release.py" "$unit_tmp_dir/config.json"',
    'printf ''{"status":"succeeded","service":"%s","releaseId":"%s"}\n'' "$service" "$release_id"',
    '# agentbridge-upload-end'
) -join "`n"
$remoteScript = $remoteTemplate.Replace("__REMOTE_WHEEL__", $remoteWheel).Replace("__REMOTE_ROOT__", $RemoteRoot).Replace("__RELEASE_ID__", $releaseId).Replace("__SERVICE_NAME__", $ServiceName).Replace("__INSTALL_SYSTEM_DEPENDENCIES__", $(if ($InstallSystemDependencies) { "1" } else { "0" }))
$remoteScript = $remoteScript.Replace("__RELEASE_RUNNER__", $releaseRunnerBase64).Replace("__RELEASE_CONFIG__", $releaseConfigBase64)
$remoteScript | & $ssh.Source -T @connectionArguments $target "bash -s"
if ($LASTEXITCODE -ne 0) {
    throw "Remote AgentBridge deployment failed"
}
} else {
    # The saved runner/config belong to this exact deployed candidate. No upload,
    # package install, service switch or business replay is permitted on resume.
    $savedRelease = "$RemoteRoot/releases/$releaseId"
    $completeCommand = "'$savedRelease/venv/bin/python' -I '$savedRelease/release.py' --complete '$savedRelease/transaction.json'"
    & $ssh.Source -T @connectionArguments $target $completeCommand
    if ($LASTEXITCODE -ne 0) { throw "Confirmed release completion failed; inspect its deployment.json receipt" }
}

if ((Get-AgentBridgeOpenClawInputs -RepoRoot $repoRoot).fingerprint -ne $restartPlan.fingerprint) {
    throw "OpenClaw inputs changed during deployment; refusing to restart unplanned inputs."
}
if ($RestartOpenClaw) {
    $openClawConfigPath = if ($env:OPENCLAW_CONFIG_PATH) {
        $env:OPENCLAW_CONFIG_PATH
    }
    else {
        Join-Path $env:USERPROFILE ".openclaw\openclaw.json"
    }
    $diagnosticsAlreadyConfigured = $false
    if (Test-Path -LiteralPath $openClawConfigPath -PathType Leaf) {
        try {
            $openClawConfig = Get-Content -LiteralPath $openClawConfigPath `
                -Raw -Encoding utf8 | ConvertFrom-Json
            $diagnosticsAlreadyConfigured = (
                $openClawConfig.diagnostics.stuckSessionWarnMs -eq 30000 -and
                $openClawConfig.diagnostics.stuckSessionAbortMs -eq 120000
            )
        }
        catch {
            $diagnosticsAlreadyConfigured = $false
        }
    }
    $diagnosticsBatch = @(
        @{
            path = "diagnostics.stuckSessionWarnMs"
            value = 30000
        },
        @{
            path = "diagnostics.stuckSessionAbortMs"
            value = 120000
        }
    ) | ConvertTo-Json -Compress
    if ($diagnosticsAlreadyConfigured) {
        Write-Host "OpenClaw stuck-session recovery is already configured; skipping config write."
    }
    else {
        $diagnosticsBatchFile = Join-Path ([IO.Path]::GetTempPath()) (
            "agentbridge-openclaw-diagnostics-{0}.json" -f [guid]::NewGuid().ToString("N")
        )
        $configExitCode = 1
        try {
            [IO.File]::WriteAllText(
                $diagnosticsBatchFile,
                $diagnosticsBatch,
                [Text.UTF8Encoding]::new($false)
            )
            & openclaw config set --batch-file $diagnosticsBatchFile
            $configExitCode = $LASTEXITCODE
        }
        finally {
            Remove-Item -LiteralPath $diagnosticsBatchFile -Force -ErrorAction SilentlyContinue
        }
        if ($configExitCode -ne 0) {
            throw "Configuring OpenClaw stuck-session recovery failed"
        }
    }
    # Guardrail writes above are intentional input changes. Freeze the final
    # inputs immediately before handing off to the serialized lifecycle.
    $gatewayInputs = Get-AgentBridgeOpenClawInputs -RepoRoot $repoRoot
    # Persist the outstanding acceptance BEFORE restart. If warm-up fails or
    # this publisher exits, the next attempt must retry acceptance, not reboot.
    [IO.File]::WriteAllText($gatewayWarmupPendingPath, $gatewayInputs.fingerprint)
    $gatewayRestart = (
        & $gatewayLifecycleScript -ReadyTimeoutSeconds 600 `
            -IfInputsChanged:(-not $ForceRestartOpenClaw) `
            -ExpectedInputFingerprint $gatewayInputs.fingerprint
    ) | Out-String | ConvertFrom-Json
    if ($gatewayRestart.status -ne "succeeded" -or
        -not $gatewayRestart.visibleForeground) {
        throw "Visible OpenClaw Gateway restart failed"
    }

    $gatewayRuntime = (
        & $gatewayRuntimeScript -RegistrationMaxAgeSeconds 300
    ) | Out-String | ConvertFrom-Json
    if ($gatewayRuntime.status -ne "succeeded" -or
        $gatewayRuntime.pluginStatus -ne "loaded") {
        throw "OpenClaw Gateway runtime or AgentBridge plugin is not healthy"
    }
    $gatewayRestartPerformed = $gatewayRestart.action -eq "restart"
}
else {
    Write-Host "OpenClaw inputs unchanged; retaining the running Gateway."
    $currentPlan = Get-AgentBridgeOpenClawRestartPlan -RepoRoot $repoRoot
    if ($currentPlan.required) {
        throw "OpenClaw runtime changed during deployment; inspect it before resuming acceptance."
    }
    & $gatewayRuntimeScript | Out-Host
}

Complete-AgentBridgeOpenClawWarmup -PendingPath $gatewayWarmupPendingPath -GetPlan {
    Get-AgentBridgeOpenClawRestartPlan -RepoRoot $repoRoot
} -Warmup {
    (& $gatewayWarmupScript) | Out-String | ConvertFrom-Json
} | Out-Host

if (-not $SkipSmoke) {
    $releaseSmokeAttempts = 6
    for ($attempt = 1; $attempt -le $releaseSmokeAttempts; $attempt++) {
        try {
            & $smokeScript -Check Release
            break
        } catch {
            if ($attempt -eq $releaseSmokeAttempts) {
                throw
            }
            Start-Sleep -Seconds 5
        }
    }
    if ($IncludeLoginReuseSmoke) {
        & $smokeScript -Check LoginReuse
    }
}

[ordered]@{
    status = "succeeded"
    releaseId = $releaseId
    target = $target
    wheel = $wheel.FullName
    service = $ServiceName
    smoke = -not $SkipSmoke
    restartOpenClaw = $gatewayRestartPerformed
    openClawRestartReason = $restartPlan.reason
    installSystemDependencies = [bool]$InstallSystemDependencies
} | ConvertTo-Json -Compress
