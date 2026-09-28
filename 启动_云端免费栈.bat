@echo off
rem =====================================================================
rem  OCV 全云端免费栈插件 —— 便捷启动入口
rem
rem  实际上插件通过 runtime\python\Lib\site-packages\ocv_cloud_free_stack.pth
rem  自动注入，用「双击启动.bat」或 OCV_Launcher.exe 也一样生效。
rem  这个脚本只是额外做三件事：
rem    1. 预先把 PYTHONPATH 设好（双保险）
rem    2. 提前把图片 shim 拉起来，避免首个分镜多等几秒
rem    3. 打开时给出当前配置状态
rem =====================================================================
setlocal EnableDelayedExpansion
set "PLUGIN_DIR=%~dp0"
set "PLUGIN_DIR=%PLUGIN_DIR:~0,-1%"
for %%I in ("%PLUGIN_DIR%\..\..") do set "ROOT_DIR=%%~fI"

set "PYTHONPATH=%PLUGIN_DIR%;%PYTHONPATH%"

echo ============================================================
echo  OCV 全云端免费栈插件
echo   插件目录: %PLUGIN_DIR%
echo   OCV 根目录: %ROOT_DIR%
echo ============================================================
echo.

if not exist "%ROOT_DIR%\runtime\python\python.exe" (
    echo [ERROR] 找不到 OCV 的便携解释器: %ROOT_DIR%\runtime\python\python.exe
    echo         请确认本插件放在 OCV 根目录的 plugins\cloud_free_stack 下。
    pause
    exit /b 1
)

echo [1/2] 配置状态
"%ROOT_DIR%\runtime\python\python.exe" "%PLUGIN_DIR%\cloud_stack_ctl.py" status
echo.
echo [2/2] 启动本地图片 shim（商汤图片链路）
"%ROOT_DIR%\runtime\python\python.exe" "%PLUGIN_DIR%\cloud_stack_ctl.py" stop >nul 2>nul
start "" /b "%ROOT_DIR%\runtime\python\python.exe" "%PLUGIN_DIR%\cloud_stack_ctl.py" shim
timeout /t 2 /nobreak >nul
echo.
echo 正在启动 OCV ...
echo.

call "%ROOT_DIR%\start_windows.bat"
exit /b %errorlevel%
