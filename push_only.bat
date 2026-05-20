@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo.
echo ========================================
echo  仅推送到 GitHub（不提交新改动）
echo ========================================
echo.

git status -sb
echo.
git push origin main
if %errorlevel% neq 0 (
    echo.
    echo [失败] 无法连接 GitHub。请：
    echo   1. 开启 VPN/系统代理后重试
    echo   2. 或在本机 PowerShell 执行：
    echo      git config --global http.proxy http://127.0.0.1:你的代理端口
    echo      git push origin main
    pause
    exit /b 1
)

echo.
echo [成功] 已推送。服务器执行：
echo   cd /www/wwwroot/MD ^&^& git pull origin main
echo.
pause
