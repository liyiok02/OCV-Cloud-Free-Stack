@echo off
rem =====================================================================
rem  OCV full-cloud free stack plugin -- UNINSTALL (double-click me)
rem
rem  WHAT THIS DOES
rem  ---------------
rem  Removes everything this plugin added to OCV, and nothing else:
rem    1. runtime\python\Lib\site-packages\ocv_cloud_free_stack.pth
rem       (the import anchor -- without it the plugin never loads)
rem    2. the marked <script> block in frontend\index.html
rem       (the "cloud free stack" button)
rem    3. the plugin block in .env, restoring the 4 OCV keys it
rem       overrode (LANGUAGE_PROVIDER / IMAGE_API_BASE_URL /
rem       IMAGE_MODEL_ID / DASHSCOPE_API_KEY). Everything else in .env
rem       is left byte-for-byte alone.
rem    4. a standalone shim process, if one happens to be running
rem    5. __pycache__ folders
rem
rem  It NEVER touches OCV source files (module*.py / backend/ /
rem  story_agents.py / ...), and never touches other plugins' injection
rem  (e.g. ocv_watermark).
rem
rem  OPTIONS
rem  -------
rem    Drag-free usage is fine: just double-click for a guided,
rem    confirm-before-acting run. For the command line:
rem      uninstall.bat --dry-run     report only, change nothing
rem      uninstall.bat --purge       also delete var\ (image cache,
rem                                  panel config, logs)
rem      uninstall.bat --disable     just disable, keep everything
rem
rem  A NOTE ABOUT THE SHIM
rem  ---------------------
rem  The image shim normally runs inside the OCV backend process as a
rem  daemon thread. Uninstalling removes its anchor and pid file, but
rem  that thread only disappears when OCV is restarted -- so the script
rem  tells you to restart OCV once. That is expected, not a leftover.
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
echo  OCV full-cloud free stack -- UNINSTALL
echo   plugin dir : %PLUGIN_DIR%
echo   OCV root   : %ROOT_DIR%
echo ============================================================
echo.

if not exist "%PLUGIN_DIR%\uninstall.py" (
    echo [ERROR] uninstall.py not found next to this script.
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
rem  --yes is NOT passed on purpose: a double-click should always ask
rem  before destroying injected state. The Python script prints the
rem  full probe report first, then waits for confirmation.
"%PY%" "%PLUGIN_DIR%\uninstall.py" %*
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" (
    echo [OK] Uninstall finished. Restart OCV once to drop the in-process
    echo      shim and the runtime patches.
) else (
    echo [WARN] Uninstall reported a problem -- read the output above.
)
echo.
pause
exit /b %RC%
