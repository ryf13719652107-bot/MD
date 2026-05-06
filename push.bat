@echo off
chcp 65001 >nul
cd /d "%~dp0"

set msg=%1
if "%msg%"=="" set /p msg="输入提交信息: "
if "%msg%"=="" set msg=update

echo.
echo ========================================
echo  推送到 GitHub
echo ========================================
echo.

git add .
git commit -m "%msg%"
if %errorlevel% neq 0 (
    echo [警告] 没有新改动或提交失败
)
git push origin main

echo.
echo ========================================
echo  完成！服务器执行 ./deploy.sh 即可更新
echo ========================================
pause
