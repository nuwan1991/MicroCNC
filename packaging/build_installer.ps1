# Build MicroPCB-Setup-<version>.exe:  powershell -File packaging\build_installer.ps1
# Needs: .venv with the project + pyinstaller, Inno Setup 6.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"
$version = & $py -c "import sys; sys.path.insert(0, 'src'); import micropcb; print(micropcb.__version__)"
Write-Host "MicroPCB $version"

& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build\pyinstaller packaging\micropcb.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
          "C:\Program Files\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 not found (winget install JRSoftware.InnoSetup)" }
& $iscc "/DAppVersion=$version" packaging\micropcb.iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
Get-Item "dist\installer\MicroPCB-Setup-$version.exe" | Select-Object FullName, Length
