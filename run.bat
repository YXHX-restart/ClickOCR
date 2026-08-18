@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到虚拟环境 .venv\Scripts\python.exe，请先按开发文档创建并安装依赖。
    pause
    exit /b 1
)

echo ============================================
echo   ClickOCR 启动中...
echo   托盘图标: 右键"截图" 或 全局热键 Ctrl+Shift+R
echo   关闭本窗口即退出程序。
echo ============================================
".venv\Scripts\python.exe" main.py
echo.
echo [ClickOCR 已退出，返回码 %errorlevel%]
echo （若提示"已在运行中"，说明已有实例，右键托盘图标"退出"后再双击本文件即可。）
pause
