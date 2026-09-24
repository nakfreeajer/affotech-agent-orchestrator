param(
    [switch]$StatusOnly,
    [string]$AuthorizeRetry
)

$ErrorActionPreference = "Stop"
$Repository = (Resolve-Path $PSScriptRoot).Path
$StateDir = Join-Path $Repository ".agent-work\orchestrator"
$Python = if (Test-Path "C:\Python314\python.exe") { "C:\Python314\python.exe" } else { "python" }
$Bootstrap = Join-Path $Repository "crash_recovery_bootstrap.py"
$Endpoint = "http://127.0.0.1:9333"
$ArchitectProfile = "C:\BraveDebug\Architect"
$ArchitectPort = 9333

function Invoke-Recovery([string[]]$Extra) {
    $raw = & $Python $Bootstrap --repository $Repository --state-dir $StateDir @Extra
    if ($LASTEXITCODE -ne 0) { throw "Recovery preflight failed: $raw" }
    return ($raw | ConvertFrom-Json)
}

function Show-Summary($Report, [string]$AuthorizationMode) {
    $s = $Report.state
    Write-Host "========================================"
    Write-Host "AFFOTECH RECOVERY BOOTSTRAP"
    Write-Host "========================================"
    Write-Host "Repository: $($Report.discovery.repository)"
    Write-Host "Head: $($Report.discovery.head)"
    Write-Host "Branch: $($Report.discovery.branch)"
    Write-Host "Working tree clean: $($Report.discovery.workingTreeClean)"
    Write-Host "Architect: $($s.architectConversationId)"
    Write-Host "Architect conversation: https://chatgpt.com/c/$($s.architectConversationId)"
    Write-Host "Watcher: $(if($Report.discovery.watcherRunning){'WATCHER_ALREADY_RUNNING'}else{'STOPPED'})"
    Write-Host "Workflow state: $($s.state)"
    Write-Host "Task: $($s.taskId)"
    Write-Host "Last completed: $($s.lastCompletedTaskId)"
    Write-Host "Executor session: $($s.executorSessionId)"
    Write-Host "Executor process: PID=$($Report.discovery.executorPid) alive=$($Report.discovery.executorPidAlive)"
    Write-Host "Recovery classification: $($Report.recoveryClassification)"
    Write-Host "Authorization mode: $AuthorizationMode"
}

$Report = Invoke-Recovery @()
$AuthorizationMode = if ($AuthorizeRetry) { "AUTHORIZE_RETRY_$AuthorizeRetry" } else { "NONE" }
Show-Summary $Report $AuthorizationMode

if ($Report.discovery.watcherRunning) {
    Write-Host "WATCHER_ALREADY_RUNNING"
    exit 0
}

if ($StatusOnly) {
    Write-Host "STATUS_ONLY: no browser, watcher, authorization, or state mutation performed."
    exit 0
}

if ($AuthorizeRetry) {
    $RetryReport = Invoke-Recovery @("--validate-retry", $AuthorizeRetry)
    if (-not $RetryReport.retryEligible) {
        Write-Host "BLOCKED: $($RetryReport.retryReason)"
        exit 3
    }
}

$Probe = $null
$CdpReason = (& $Python -c "from crash_recovery_bootstrap import architect_cdp_health; print(architect_cdp_health('$Endpoint')[1])").Trim()
if ($CdpReason -eq "ARCHITECT_CDP_HEALTHY") {
    $Probe = Invoke-Recovery @("--probe-architect", "--endpoint", $Endpoint)
} elseif ($CdpReason -ne "ARCHITECT_CDP_ABSENT") {
    Write-Host "BLOCKED: ARCHITECT_CDP_UNVERIFIED"
    exit 8
}
if ($Probe -and $Probe.recoveryClassification -eq "ARCHITECT_RECOVERY_INCONCLUSIVE") {
    Write-Host "ARCHITECT_RECOVERY_INCONCLUSIVE"
    exit 4
}

if ($Report.recoveryClassification -eq "HUMAN_REQUIRED_NO_AUTOMATIC_ACTION" -and -not $AuthorizeRetry) {
    Write-Host "HUMAN_REQUIRED: use -AuthorizeRetry <current taskId> only after human review."
    exit 5
}

if ($Probe -eq $null -or -not $Probe.architectObservation) {
    if ($CdpReason -eq "ARCHITECT_CDP_ABSENT") {
        $url = "https://chatgpt.com/c/$($Report.state.architectConversationId)"
        $brave = @(
            "C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
            "C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $brave) { Write-Host "BLOCKED: BRAVE_EXECUTABLE_NOT_FOUND"; exit 6 }
        Write-Host "Launching governed Brave Architect profile visibly..."
        Start-Process -FilePath $brave -ArgumentList @("--remote-debugging-port=$ArchitectPort", "--user-data-dir=$ArchitectProfile", $url)
        $ready = $false
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            try { Invoke-WebRequest "$Endpoint/json/version" -TimeoutSec 2 | Out-Null; $ready = $true; break } catch {}
        }
        if (-not $ready) { Write-Host "BLOCKED: ARCHITECT_CDP_READINESS_TIMEOUT"; exit 7 }
        $Probe = Invoke-Recovery @("--probe-architect", "--endpoint", $Endpoint)
    } elseif ($reason -ne "ARCHITECT_CDP_HEALTHY") {
        Write-Host "BLOCKED: ARCHITECT_CDP_UNVERIFIED"; exit 8
    }
}

$answer = Read-Host "Type START to launch the visible resident watcher (anything else exits)"
if ($answer -ne "START") { Write-Host "No action taken."; exit 0 }

Write-Host "Starting resident watcher visibly in this terminal."
$oldAuth = $env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY
try {
    if ($AuthorizeRetry) { $env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY = $AuthorizeRetry }
    else { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY -ErrorAction SilentlyContinue }
    & $Python (Join-Path $Repository "local_orchestrator_watcher.py")
    exit $LASTEXITCODE
} finally {
    if ($null -eq $oldAuth) { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY -ErrorAction SilentlyContinue }
    else { $env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY = $oldAuth }
}
