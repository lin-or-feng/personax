# PersonaX 一键环境安装脚本（Windows PowerShell 5.1 / 7 通用）
# 用法：在项目根目录执行  .\setup.ps1
# 说明：PowerShell 5.1 不支持 bash 的 && 语法，本脚本把安装步骤拆开执行。
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host " PersonaX 环境安装（1/3 → 3/3）" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan

Write-Host "`n[1/3] 安装核心依赖（openai/pydantic/pyyaml/langgraph）..." -ForegroundColor Yellow
python -m pip install -e .

Write-Host "`n[2/3] 安装 Playwright（真实发布浏览器驱动）..." -ForegroundColor Yellow
python -m pip install playwright

Write-Host "`n[3/3] 下载 Chromium 浏览器内核（约 150MB，只需一次）..." -ForegroundColor Yellow
Write-Host "     提示：国内直连 cdn.playwright.dev 很慢，已自动切换到 npmmirror 镜像。" -ForegroundColor DarkGray
if (-not $env:PLAYWRIGHT_DOWNLOAD_HOST) {
    $env:PLAYWRIGHT_DOWNLOAD_HOST = "https://npmmirror.com/mirrors/playwright/"
}
python -m playwright install chromium

Write-Host ""
Write-Host "（如果下载仍慢，可跳过本步，直接用系统 Chrome：所有命令加 --browser chrome）" -ForegroundColor DarkGray

Write-Host ""
Write-Host "======================================" -ForegroundColor Green
Write-Host " 环境就绪 ✅  接下来三步：" -ForegroundColor Green
Write-Host "======================================" -ForegroundColor Green
Write-Host "  1. 干跑一篇（不真发）:  python main.py generate --topic `"秋招穿搭`""
Write-Host "  2. 登录小红书（一次性）:  python main.py login"
Write-Host "  3. 真实发布:  python main.py publish --draft content_bank/example.json --real"
Write-Host ""
Write-Host "  （可选）可视化工作台（生成/编辑/检验/定时发布）："
Write-Host "  python -m pip install streamlit"
Write-Host "  python -m streamlit run app.py"
Write-Host ""
Write-Host "（可选）配置 DeepSeek 真实写作："
Write-Host "  PowerShell:  `$env:DEEPSEEK_API_KEY = `"sk-你的key`""
Write-Host "  永久生效:  setx DEEPSEEK_API_KEY `"sk-你的key`""
