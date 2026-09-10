@echo off
chcp 65001 >nul
rem ClickOCR 启动（使用相对路径定位，ClickOCR 文件夹可整体移动到任意位置）
rem   python = <本脚本目录>.venv\Scripts\python.exe
rem   main.py = <本脚本目录>\main.py
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" main.py
pause