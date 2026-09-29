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
$defaultManagerUrl = "$ServerUrl/api/worker-assets/manager"
if (-not $ManagerUrl) { $ManagerUrl = $defaultManagerUrl }
$workerUrl = "$ServerUrl/api/worker-assets/worker"
$workerExe = Join-Path $InstallDir "DoubaoWorker.exe"
$configPath = Join-Path $InstallDir "config.json"
$managerDir = Join-Path $InstallDir "manager"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

if (-not (Test-Path $workerExe)) {
    $downloadHeaders = @{ "X-Worker-Enrollment-Token" = $EnrollToken }
    $manifest = Invoke-RestMethod -Uri "$workerUrl/manifest" -Headers $downloadHeaders
    if (-not $manifest.sha256) { throw "Worker package manifest has no SHA-256." }
    Write-Host "Downloading Worker $WorkerVersion..."
    Invoke-WebRequest -Uri $workerUrl -Headers $downloadHeaders -OutFile $workerExe -UseBasicParsing
    $actual = (Get-FileHash $workerExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $manifest.sha256.ToLowerInvariant()) {
        Remove-Item $workerExe -Force
        throw "Worker package SHA-256 mismatch."
    }
}

function Find-ManagerExe {
    if (-not (Test-Path $managerDir)) { return $null }
    $found = Get-ChildItem -Path $managerDir -Filter "豆包管理器.exe" -File -Recurse -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
    if (-not $found) {
        $found = Get-ChildItem -Path $managerDir -Filter "*.exe" -File -ErrorAction SilentlyContinue |
            Sort-Object Length -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    return $found
}

$managerExe = Find-ManagerExe
if (-not $managerExe) {
    $archive = Join-Path $InstallDir "manager.zip"
    Write-Host "Downloading Doubao manager..."
    $downloadHeaders = @{}
    if ($ManagerUrl -eq $defaultManagerUrl) {
        $downloadHeaders["X-Worker-Enrollment-Token"] = $EnrollToken
        $manifest = Invoke-RestMethod -Uri "$ManagerUrl/manifest" -Headers $downloadHeaders
        if (-not $ManagerSha256) { $ManagerSha256 = $manifest.sha256 }
    }
    Invoke-WebRequest -Uri $ManagerUrl -Headers $downloadHeaders -OutFile $archive -UseBasicParsing
    if ($ManagerSha256) {
        $actual = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $ManagerSha256.ToLowerInvariant()) {
            throw "Manager archive SHA-256 mismatch."
        }
    }
    Expand-Archive -Path $archive -DestinationPath $managerDir -Force
    Remove-Item $archive -Force
    $managerExe = Find-ManagerExe
}

if (-not $managerExe) {
    throw "Doubao manager was not found in the downloaded package."
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
