$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Build on a Windows x64 cloud machine without requiring Python to be installed.
$pythonVersion = '3.11.9'
$pythonZip = "python-$pythonVersion-embed-amd64.zip"
$pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/$pythonZip"
$buildRoot = Join-Path $PSScriptRoot '.portable-build'
$pythonRoot = Join-Path $buildRoot 'python'
$zipPath = Join-Path $buildRoot $pythonZip
$getPipPath = Join-Path $buildRoot 'get-pip.py'
$pythonExe = Join-Path $pythonRoot 'python.exe'

New-Item -ItemType Directory -Force -Path $buildRoot | Out-Null

if (-not (Test-Path $pythonExe)) {
    if (-not (Test-Path $zipPath)) {
        Write-Host "Downloading Python $pythonVersion..."
        Invoke-WebRequest -UseBasicParsing -Uri $pythonUrl -OutFile $zipPath
    }
    New-Item -ItemType Directory -Force -Path $pythonRoot | Out-Null
    Expand-Archive -Force -Path $zipPath -DestinationPath $pythonRoot

    # The embeddable distribution disables site-packages by default.
    $pth = Get-ChildItem $pythonRoot -Filter '*._pth' | Select-Object -First 1
    if ($null -eq $pth) {
        throw 'Python embeddable package did not contain a ._pth file.'
    }
    (Get-Content $pth.FullName) -replace '^#import site$', 'import site' |
        Set-Content -Encoding ascii $pth.FullName
}

if (-not (Test-Path $getPipPath)) {
    Write-Host 'Downloading pip bootstrap script...'
    Invoke-WebRequest -UseBasicParsing `
        -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getPipPath
}

Write-Host 'Installing build dependencies...'
& $pythonExe $getPipPath --disable-pip-version-check
& $pythonExe -m pip install --disable-pip-version-check --upgrade `
    -r (Join-Path $PSScriptRoot 'requirements.txt') `
    'pyinstaller>=6,<7'

$dist = Join-Path $PSScriptRoot 'dist'
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $dist
New-Item -ItemType Directory -Force -Path $dist | Out-Null

Write-Host 'Building DoubaoWorker.exe...'
& $pythonExe -m PyInstaller --noconfirm --clean --onefile `
    --name DoubaoWorker `
    --hidden-import websocket `
    --collect-all qiniu `
    --add-data ((Join-Path $PSScriptRoot 'plugin') + ';plugin') `
    --add-data ((Join-Path $PSScriptRoot 'VERSION') + ';.') `
    --distpath $dist `
    (Join-Path $PSScriptRoot 'agent.py')

$pluginDist = Join-Path $dist 'plugin'
New-Item -ItemType Directory -Force -Path $pluginDist | Out-Null
Copy-Item -Path (Join-Path $PSScriptRoot 'plugin\*') -Destination $pluginDist -Recurse -Force
Copy-Item -Path (Join-Path $PSScriptRoot 'config.example.json') -Destination (Join-Path $dist 'config.example.json') -Force
if (Test-Path (Join-Path $PSScriptRoot 'VERSION')) {
    Copy-Item -Path (Join-Path $PSScriptRoot 'VERSION') -Destination (Join-Path $dist 'VERSION') -Force
}

Write-Host ''
Write-Host "Built: $dist\DoubaoWorker.exe"
Write-Host 'Next: copy config.example.json to config.json, fill in the token, then run DoubaoWorker.exe.'
