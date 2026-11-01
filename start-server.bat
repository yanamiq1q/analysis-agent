@echo off
REM ============================================================
REM  启动 data-agent HTTP 服务（含 /mcp 端点，供 WorkBuddy 连接）
REM
REM  注意：这个窗口就是服务进程。
REM        关掉窗口 = 服务停止 = WorkBuddy 会报
REM        "fetch failed (connect ECONNREFUSED 127.0.0.1:8000)"
REM ============================================================
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [x] 没找到 .venv，请先按 README 创建虚拟环境并安装依赖
    pause
    exit /b 1
)

echo.
echo   data-agent  http://127.0.0.1:8000
echo   MCP 端点    http://127.0.0.1:8000/mcp
echo   表清单      http://127.0.0.1:8000/api/tables
echo.
echo   保持本窗口打开；按 Ctrl+C 停止服务。
echo.

".venv\Scripts\python.exe" -m uvicorn api.server:app --host 127.0.0.1 --port 8000
pause
