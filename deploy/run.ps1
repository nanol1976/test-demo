# Windows 一键启动脚本
# 用法：powershell -ExecutionPolicy Bypass -File deploy\run.ps1
#     可选参数：-Port 8000  -Token "xxx"  -Bind 0.0.0.0
#     -Bind 默认 127.0.0.1（只本机可见）；要看手机，传 -Bind 0.0.0.0（局域网可见，务必先设 API_TOKEN）
param(
    [int]$Port = 8000,
    [string]$Token = "",
    [string]$Bind = "127.0.0.1"
)

# 控制台按 UTF-8 输出，避免中文提示在日志里变乱码
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$env:PYTHONPATH = $Root

Write-Host "工作目录：$Root"

# 1) 找解释器：优先 .venv311（本项目在 Windows 上验证过的唯一可用解释器）
$Py = Join-Path $Root ".venv311\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "未找到 .venv311，尝试创建…"
    $SysPy = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $SysPy) { Write-Host "找不到 python，请先安装 Python 3.11+"; exit 1 }
    & $SysPy -m venv .venv311
    if (-not (Test-Path $Py)) { Write-Host "创建虚拟环境失败"; exit 1 }
}

# 2) 检查依赖（已满足则跳过安装，避免镜像限流时卡住）
Write-Host "检查依赖…"
& $Py -c "import fastapi, uvicorn, sqlalchemy, pydantic_settings"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  安装依赖…"
    & $Py -m pip install -q -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Host "依赖安装失败"; exit 1 }
} else {
    Write-Host "  依赖已满足，跳过安装"
}

# 3) 迁移
Write-Host "执行迁移…"
& $Py deploy\migrate.py
if ($LASTEXITCODE -ne 0) { Write-Host "迁移失败"; exit 1 }

# 4) 起服务
if ($Token) { $env:API_TOKEN = $Token }
if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "sqlite:///./data/mvp.db" }
if (-not $env:ARCHIVE_ROOT) { $env:ARCHIVE_ROOT = "./archive" }
New-Item -ItemType Directory -Force "$Root\data" | Out-Null

Write-Host ""
Write-Host "看板地址：http://127.0.0.1:$Port/"
Write-Host "接口文档：http://127.0.0.1:$Port/docs"
if ($Bind -ne "127.0.0.1") {
    $lan = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254*" -and $_.PrefixLength -eq 24 } |
        Select-Object -First 1).IPAddress
    if ($lan) {
        Write-Host "手机访问：http://${lan}:$Port/   （同一 Wi-Fi；还需防火墙放行 $Port）"
    } else {
        Write-Host "手机访问：http://<本机局域网IP>:$Port/   （同一 Wi-Fi；还需防火墙放行 $Port）"
    }
    if (-not $env:API_TOKEN) {
        Write-Host "警告：绑定到 $Bind 但 API_TOKEN 为空 —— 同网段任何人都能下发指令。请在 .env 里设 API_TOKEN。"
    }
}
Write-Host "按 Ctrl+C 停止"
Write-Host ""

& $Py -m uvicorn app.main:app --host $Bind --port $Port
