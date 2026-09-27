param(
    [switch]$StatusOnly,
    [switch]$Qualification,
    [switch]$RealBrowser,
    [string]$AuthorizeRetry,
    [switch]$AuthorizeRolloverDiagnosticRetry,
    [switch]$AuthorizeRolloverPostfixQualification,
    [switch]$AuthorizeRolloverSentResponseRetry
)

$ErrorActionPreference = "Stop"
$Repository = (Resolve-Path $PSScriptRoot).Path
$StateDir = Join-Path $Repository ".agent-work\orchestrator"
$Python = if (Test-Path "C:\Python314\python.exe") { "C:\Python314\python.exe" } else { "python" }
$PythonVersionCheck = & $Python -c "import sys; print('%d.%d.%d' % sys.version_info[:3]); raise SystemExit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "Unsupported Python version ($PythonVersionCheck). AFFOTECH Orchestrator requires Python 3.10 or newer; no recovery or watcher action was started."
}
$Bootstrap = Join-Path $Repository "crash_recovery_bootstrap.py"
$Endpoint = "http://127.0.0.1:9333"
$ArchitectProfile = "C:\BraveDebug\Architect"
$ArchitectPort = 9333

if ((@([bool]$AuthorizeRetry, [bool]$AuthorizeRolloverDiagnosticRetry, [bool]$AuthorizeRolloverPostfixQualification, [bool]$AuthorizeRolloverSentResponseRetry) | Where-Object { $_ }).Count -gt 1) {
    throw "Choose only one authorization surface; Executor retry, diagnostic retry, post-fix qualification, and sent-response retry are separate."
}

if ($Qualification) {
    if ($StatusOnly -or $AuthorizeRetry -or $AuthorizeRolloverDiagnosticRetry -or $AuthorizeRolloverPostfixQualification -or $AuthorizeRolloverSentResponseRetry) {
        throw "-Qualification is isolated and cannot be combined with StatusOnly or any production authorization."
    }
    if ($RealBrowser) {
        Write-Host "QUALIFICATION: disposable real-browser pages, isolated synthetic state, protected production targets, fake Executor only."
        & $Python (Join-Path $Repository "orchestrator_real_browser_qualification.py") --run $Repository $Endpoint
    } else {
        Write-Host "QUALIFICATION: synthetic isolated state and test DOM only; no production state/CDP/browser or Executor access."
        & $Python (Join-Path $Repository "orchestrator_qualification.py") --run $Repository
    }
    exit $LASTEXITCODE
}
if ($RealBrowser) { throw "-RealBrowser is qualification-only; use -Qualification -RealBrowser." }

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
    Write-Host "Discussion pause: $(if($s.discussionPauseActive -eq $true){'ACTIVE'}else{'INACTIVE'})"
    Write-Host "Task: $($s.taskId)"
    Write-Host "Last completed: $($s.lastCompletedTaskId)"
    Write-Host "Executor session: $($s.executorSessionId)"
    Write-Host "Executor process: PID=$($Report.discovery.executorPid) alive=$($Report.discovery.executorPidAlive)"
    Write-Host "Recovery classification: $($Report.recoveryClassification)"
    Write-Host "Authorization mode: $AuthorizationMode"
}

$Report = Invoke-Recovery @()
$AuthorizationMode = if ($AuthorizeRetry) { "AUTHORIZE_EXECUTOR_RETRY_$AuthorizeRetry" } elseif ($AuthorizeRolloverDiagnosticRetry) { "AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY" } elseif ($AuthorizeRolloverPostfixQualification) { "AUTHORIZE_ROLLOVER_POSTFIX_QUALIFICATION" } elseif ($AuthorizeRolloverSentResponseRetry) { "AUTHORIZE_ROLLOVER_SENT_RESPONSE_RETRY" } else { "NONE" }
Show-Summary $Report $AuthorizationMode

if ($Report.discovery.watcherRunning) {
    Write-Host "WATCHER_ALREADY_RUNNING"
    exit 0
}

$RolloverRetryReport = $null
if ($AuthorizeRolloverDiagnosticRetry) {
    $RolloverRetryReport = Invoke-Recovery @("--validate-rollover-diagnostic-retry")
    Write-Host "Rollover diagnostic retry eligible: $($RolloverRetryReport.rolloverDiagnosticRetryEligible) reason=$($RolloverRetryReport.rolloverDiagnosticRetryReason)"
}
$PostfixQualificationReport = $null
if ($AuthorizeRolloverPostfixQualification) {
    $PostfixQualificationReport = Invoke-Recovery @("--validate-rollover-postfix-qualification")
    Write-Host "Rollover post-fix qualification eligible: $($PostfixQualificationReport.rolloverPostfixQualificationEligible) reason=$($PostfixQualificationReport.rolloverPostfixQualificationReason)"
}
$SentResponseRetryReport = $null
if ($AuthorizeRolloverSentResponseRetry) {
    $SentResponseRetryReport = Invoke-Recovery @("--validate-rollover-sent-response-retry")
    Write-Host "Rollover sent-response retry eligible: $($SentResponseRetryReport.rolloverSentResponseRetryEligible) reason=$($SentResponseRetryReport.rolloverSentResponseRetryReason)"
}

if ($StatusOnly) {
    Write-Host "STATUS_ONLY: no browser, watcher, authorization, or state mutation performed."
    exit 0
}

if ($AuthorizeRolloverDiagnosticRetry -and -not $RolloverRetryReport.rolloverDiagnosticRetryEligible) {
    Write-Host "BLOCKED: $($RolloverRetryReport.rolloverDiagnosticRetryReason)"
    exit 3
}
if ($AuthorizeRolloverPostfixQualification -and -not $PostfixQualificationReport.rolloverPostfixQualificationEligible) {
    Write-Host "BLOCKED: $($PostfixQualificationReport.rolloverPostfixQualificationReason)"
    exit 3
}
if ($AuthorizeRolloverSentResponseRetry -and -not $SentResponseRetryReport.rolloverSentResponseRetryEligible) {
    Write-Host "BLOCKED: $($SentResponseRetryReport.rolloverSentResponseRetryReason)"
    exit 3
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

if ($Report.recoveryClassification -eq "HUMAN_REQUIRED_NO_AUTOMATIC_ACTION" -and -not $AuthorizeRetry -and -not $AuthorizeRolloverDiagnosticRetry -and -not $AuthorizeRolloverPostfixQualification -and -not $AuthorizeRolloverSentResponseRetry) {
    Write-Host "HUMAN_REQUIRED: no automatic action. Executor retry and each rollover recovery qualification require separate explicit authorizations."
    exit 5
}
if ($Report.recoveryClassification -eq "SAFE_EXISTING_HANDOVER_RELAY") {
    Write-Host "SAFE_EXISTING_HANDOVER_RELAY: exact persisted epoch-5 relay is eligible; no authorization or resend will occur. Type START only to begin the normal watcher."
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
    } elseif ($CdpReason -eq "ARCHITECT_CDP_HEALTHY") {
        Write-Host "BLOCKED: ARCHITECT_CDP_PROBE_UNAVAILABLE"; exit 8
    } else {
        Write-Host "BLOCKED: ARCHITECT_CDP_UNVERIFIED"; exit 8
    }
}

$answer = Read-Host "Type START to launch the visible resident watcher (anything else exits)"
if ($answer -ne "START") { Write-Host "No action taken."; exit 0 }

Write-Host "Starting resident watcher visibly in this terminal."
$oldAuth = $env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY
$oldRolloverAuth = $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY
$oldPostfixQualificationAuth = $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_POSTFIX_QUALIFICATION
$oldSentResponseRetryAuth = $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_SENT_RESPONSE_RETRY
$oldRuntimeContext = $env:AFFOTECH_RUNTIME_CONTEXT
try {
    $env:AFFOTECH_RUNTIME_CONTEXT = "PRODUCTION"
    if ($AuthorizeRetry) { $env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY = $AuthorizeRetry }
    else { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY -ErrorAction SilentlyContinue }
    if ($AuthorizeRolloverDiagnosticRetry) { $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY = "7fdbd798659f42295a18dd2d" }
    else { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY -ErrorAction SilentlyContinue }
    if ($AuthorizeRolloverPostfixQualification) { $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_POSTFIX_QUALIFICATION = "7fdbd798659f42295a18dd2d:3" }
    else { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_POSTFIX_QUALIFICATION -ErrorAction SilentlyContinue }
    if ($AuthorizeRolloverSentResponseRetry) { $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_SENT_RESPONSE_RETRY = "7fdbd798659f42295a18dd2d:4:41db427219d5b7e9e139ee2b4164a047c46b2160e835f1303422995767c1b9b0:70D6ECCAB4FED573CD03C4DDF3867073E087C47927EBDF25DBFC63554F6EDE85" }
    else { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_SENT_RESPONSE_RETRY -ErrorAction SilentlyContinue }
    & $Python (Join-Path $Repository "local_orchestrator_watcher.py")
    exit $LASTEXITCODE
} finally {
    if ($null -eq $oldAuth) { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY -ErrorAction SilentlyContinue }
    else { $env:ORCHESTRATOR_AUTHORIZE_POSTLAUNCH_RETRY = $oldAuth }
    if ($null -eq $oldRolloverAuth) { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY -ErrorAction SilentlyContinue }
    else { $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_DIAGNOSTIC_RETRY = $oldRolloverAuth }
    if ($null -eq $oldPostfixQualificationAuth) { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_POSTFIX_QUALIFICATION -ErrorAction SilentlyContinue }
    else { $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_POSTFIX_QUALIFICATION = $oldPostfixQualificationAuth }
    if ($null -eq $oldSentResponseRetryAuth) { Remove-Item Env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_SENT_RESPONSE_RETRY -ErrorAction SilentlyContinue }
    else { $env:ORCHESTRATOR_AUTHORIZE_ROLLOVER_SENT_RESPONSE_RETRY = $oldSentResponseRetryAuth }
    if ($null -eq $oldRuntimeContext) { Remove-Item Env:AFFOTECH_RUNTIME_CONTEXT -ErrorAction SilentlyContinue }
    else { $env:AFFOTECH_RUNTIME_CONTEXT = $oldRuntimeContext }
}
