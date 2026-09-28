@echo off
rem =====================================================================
rem  OCV full-cloud free stack plugin -- re-inject after a software update
rem
rem  WHY THIS EXISTS
rem  ---------------
rem  Every OCV software update replaces the whole frontend/ folder. The
rem  launcher archives an update.json listing protected_data:
rem      .env, runtime, output, workspace, runtime_logs,
rem      user presets, third-party plugins
rem  frontend/ is NOT in that list -- it is a source folder that gets
rem  swapped out. So the <script> tag this plugin injects into
rem  frontend/index.html disappears on every update and the "cloud free
rem  stack" button vanishes from the UI. Everything else of the plugin
rem  (MiMo dubbing / SenseNova LLM / SenseNova image) keeps working,
rem  because the takeover happens at runtime via the .pth anchor, not by
rem  editing OCV source files.
rem
rem  The damage is therefore limited to *derived* injection state, and
rem  derived state can always be rebuilt in place. This script rebuilds it.
rem
rem  WHAT IT DOES
rem  ------------
rem    1. verifies / recreates runtime\python\Lib\site-packages\
rem       ocv_cloud_free_stack.pth  (the import anchor)
rem    2. verifies / recreates the injection block in frontend\index.html
rem    3. prints a before/after report
rem  It is idempotent -- run it as often as you like, it only touches
rem  files that are actually wrong.
rem
rem  YOU USUALLY DO NOT NEED THIS
rem  ----------------------------
rem  The plugin self-heals: OCV's backend process re-injects on every
rem  start (see CLOUD_STACK_AUTO_REINJECT in .env, on by default). Use
rem  this script only when the automatic path cannot run -- runtime\
rem  deleted, anchor removed by hand, or the portable folder was moved
rem  to another machine.
rem
rem  ASCII ONLY ON PURPOSE: cmd here runs at code page 936 (GBK), so
rem  UTF-8 Chinese text inside a .bat renders as mojibake.
rem =====================================================================
setlocal EnableExtensions
set "PLUGIN_DIR=%~dp0"
set "PLUGIN_DIR=%PLUGIN_DIR:~0,-1%"
for %%I in ("%PLUGIN_DIR%\..\..") do set "ROOT_DIR=%%~fI"
set "PYTHONPATH=%PLUGIN_DIR%;%PYTHONPATH%"

echo ============================================================
echo  OCV full-cloud free stack -- re-inject
echo   plugin dir : %PLUGIN_DIR%
echo   OCV root   : %ROOT_DIR%
echo ============================================================
echo.

if not exist "%PLUGIN_DIR%\cloud_stack_ctl.py" (
    echo [ERROR] cloud_stack_ctl.py not found next to this script.
    echo         This .bat must live in plugins\cloud_free_stack\.
    pause
    exit /b 1
)

set "PY=%ROOT_DIR%\runtime\python\python.exe"
if exist "%PY%" goto :run
set "PY=python"
where python >nul 2>nul
if not errorlevel 1 goto :run
set "PY=py"
where py >nul 2>nul
if not errorlevel 1 goto :run

echo [ERROR] No Python interpreter found.
echo         Expected the portable one at:
echo           %ROOT_DIR%\runtime\python\python.exe
echo         Make sure this plugin sits at OCV_ROOT\plugins\cloud_free_stack.
pause
exit /b 1

:run
echo [runner] %PY%
echo.
"%PY%" "%PLUGIN_DIR%\cloud_stack_ctl.py" reinject
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" (
    echo [OK] Derived injection state is healthy.
) else (
    echo [WARN] Some step above did not complete -- read the report.
)
echo.
echo If the button is still missing in the browser, hard-refresh the page
echo ^(Ctrl+F5^). The Vite dev server reads index.html on every request.
echo.
pause
exit /b %RC%
