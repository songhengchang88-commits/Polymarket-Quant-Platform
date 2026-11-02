@echo off
chcp 65001 >nul
title Polymarket 量化看板服务

cd /d "E:\Polymarket-Quant-Platform"

:: 检查 8888 端口是否已在运行
netstat -ano | findstr ":8888" >nul
if %errorlevel% == 0 (
    echo [INFO] Polymarket 服务已在后台运行中，直接打开浏览器...
    start http://127.0.0.1:8888
    exit /b
)

echo [INFO] 正在启动 Polymarket 量化平台后端服务...
start "" http://127.0.0.1:8888
python server.py
