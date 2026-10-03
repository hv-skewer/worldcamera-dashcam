# Build live_view.exe (double-click to run, all features)
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root "tools"
$work  = Join-Path $root "packaging\build"
$out   = Join-Path $root "bin"
$toolsPy = Join-Path $root "tools\live_view.py"

# Locate the ocl_env python (with pyinstaller + numpy + pillow)
$envPY = $env:PY
if (-not $envPY) {
    $envPY = "C:\Users\MarbleVessel\3D Objects\DSH\01-dashcam\03-gongju\ocl_env\Scripts\python.exe"
}
if (-not (Test-Path $envPY)) {
    # Fallback: try any python that has pyinstaller
    $candidates = @("python", "py -3.12", "py -3.11", "python3")
    $envPY = $null
    foreach ($c in $candidates) {
        try {
            & $c -c "import PyInstaller" 2>$null
            if ($LASTEXITCODE -eq 0) { $envPY = $c; break }
        } catch {}
    }
    if (-not $envPY) {
        Write-Error "No python with PyInstaller found. Set `$env:PY first, e.g. `$env:PY='...\<venv>\Scripts\python.exe'"
    }
}

Write-Host "==> using interpreter: $envPY"
Write-Host "==> cleaning old artifacts"
if (Test-Path $work) { Remove-Item $work -Recurse -Force }
if (Test-Path $out)  { Remove-Item $out  -Recurse -Force }
New-Item $out -ItemType Directory -Force | Out-Null

Write-Host "==> running PyInstaller (2-5 min)"
# Build a single-file console exe with all tools bundled
# extra-paths so PyInstaller can find chroma_restore.py and usb_desc.py
& $envPY -m PyInstaller `
    --name live_view `
    --onefile --console `
    --distpath $out `
    --workpath $work `
    --specpath $work `
    --paths $tools `
    --hidden-import numpy `
    --hidden-import PIL `
    --hidden-import PIL.Image `
    $toolsPy `
    2>&1 | Write-Host

if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed (exit $LASTEXITCODE)"
}

Write-Host ""
$exe = Join-Path $out "live_view.exe"
Write-Host "==> done: $exe"
Write-Host "    size: $([math]::Round((Get-Item $exe).Length/1MB, 1)) MB"
Write-Host ""
Write-Host "try it:"
Write-Host "    $exe"
Write-Host "    $exe --probe"
Write-Host "    $exe --serve"
