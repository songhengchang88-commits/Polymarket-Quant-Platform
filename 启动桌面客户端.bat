@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================================
echo   PolyArbitrage Pro - Polymarket CLOB V2 原生桌面终端
echo ========================================================
echo 正在启动独立量化客户端窗口...
start "" pythonw app_desktop.py
exit
