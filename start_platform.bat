@echo off
chcp 65001 >nul
title Polymarket 5分钟量化实战指挥舰 v3.6

cd /d "E:\Polymarket-Quant-Platform"

:: 检查 18899 端口是否已在运行
netstat -ano | findstr ":18899" >nul
if %errorlevel% == 0 (
    echo [INFO] Polymarket 5分钟量化指挥舰已在后台运行中，直接打开浏览器看板...
    start http://127.0.0.1:18899
    exit /b
)

echo [INFO] 正在启动 Polymarket 5分钟加密量化指挥舰服务 (18899)...
start "" http://127.0.0.1:18899
python crypto_5m_hud.py
