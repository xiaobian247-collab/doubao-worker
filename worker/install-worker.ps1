param(
    [string]$ServerUrl = "https://www.zcbox.top",
    [string]$EnrollToken = $env:DOUBAO_WORKER_ENROLL_TOKEN,
    [string]$ManagerUrl = $env:DOUBAO_MANAGER_URL,
    [string]$ManagerSha256 = $env:DOUBAO_MANAGER_SHA256,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "DoubaoWorker"),
    [string]$WorkerVersion = "0.1.1"
)

$ErrorActionPreference = "Stop"
if (-not $EnrollToken) {
    throw "Missing enrollment token. Set DOUBAO_WORKER_ENROLL_TOKEN or pass -EnrollToken."
}

$ServerUrl = $ServerUrl.TrimEnd('/')
$workerExe = Join-Path $InstallDir "DoubaoWorker.exe"
$configPath = Join-Path $InstallDir "config.json"
$managerDir = Join-Path $InstallDir "manager"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

if (-not (Test-Path $workerExe)) {
    $url = "https://github.com/xiaobian247-collab/doubao-worker/releases/download/worker-v$WorkerVersion/DoubaoWorker.exe"
    Write-Host "Downloading Worker $WorkerVersion..."
    Invoke-WebRequest -Uri $url -OutFile $workerExe -UseBasicParsing
}

if ($ManagerUrl -and -not (Test-Path $managerDir)) {
    $archive = Join-Path $InstallDir "manager.zip"
    Write-Host "Downloading Doubao manager..."
    Invoke-WebRequest -Uri $ManagerUrl -OutFile $archive -UseBasicParsing
    if ($ManagerSha256) {
        $actual = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $ManagerSha256.ToLowerInvariant()) {
            throw "Manager archive SHA-256 mismatch."
        }
    }
    Expand-Archive -Path $archive -DestinationPath $managerDir -Force
    Remove-Item $archive -Force
}

$managerExe = Get-ChildItem -Path $managerDir -Filter "豆包管理器.exe" -File -Recurse -ErrorAction SilentlyContinue |
    Select-Object -First 1 -ExpandProperty FullName
if (-not $managerExe) {
    $managerExe = Get-ChildItem -Path $managerDir -Filter "*.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $managerExe) {
    throw "Doubao manager was not found. Pass -ManagerUrl with a zip containing 豆包管理器.exe."
}

if (Test-Path $configPath) {
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
    Write-Host "Existing Worker identity: $($config.worker_id)"
} else {
    $workerId = "wkr-$([guid]::NewGuid().ToString())"
    $body = @{ worker_id = $workerId; machine_name = $env:COMPUTERNAME } | ConvertTo-Json
    Write-Host "Registering $workerId..."
    $registration = Invoke-RestMethod -Method Post -Uri "$ServerUrl/api/workers/register" `
        -Headers @{ "X-Worker-Enrollment-Token" = $EnrollToken } `
        -ContentType "application/json" -Body $body
    $config = [ordered]@{
        server_url = $ServerUrl
        worker_id = $registration.worker_id
        worker_token = $registration.worker_token
        manager_exe = $managerExe
        manager_port = 9223
        auto_update = $true
    }
    $json = $config | ConvertTo-Json
    [System.IO.File]::WriteAllText($configPath, $json, (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Registered $($registration.worker_id)."
}

Write-Host "Checking Worker..."
Push-Location $InstallDir
try {
    & $workerExe --check
    if ($LASTEXITCODE -ne 0) { throw "Worker check failed." }
    $taskName = "Doubao Worker"
    $action = "`"$workerExe`""
    schtasks.exe /Create /TN $taskName /SC ONLOGON /TR $action /F | Out-Host
    Start-Process -FilePath $workerExe -WorkingDirectory $InstallDir
    Write-Host "Worker started. It will also start at user logon."
} finally {
    Pop-Location
}
