# 一键装依赖（Windows PowerShell）
# 核心功能（--probe / --serve / --photo / --record）不需要装任何东西。
# 可选功能（--crop / --chroma）需要 pillow + numpy。
$py = "python"
# 如果默认 python 不是 3.7+，提示用户换
& $py -c "import sys; assert sys.version_info >= (3,7), 'need python 3.7+'" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "!! 默认 python 版本不对，请用 'py -3.12 setup_env.ps1' 或 'python3 setup_env.ps1'"
    exit 1
}
& $py -m pip install -r requirements.txt
Write-Host ""
Write-Host "依赖装好了。自检："
Write-Host "  python tools\live_view.py --probe"
