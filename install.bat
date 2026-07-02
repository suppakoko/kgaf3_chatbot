@echo off
REM ===========================================================================
REM install.bat - one-click installer for afmm_chat (Lite) on Windows.
REM
REM Strategy (spec 06 6.4): Windows = Docker Desktop + WSL2. This .bat checks
REM those prerequisites, then runs the SAME install.sh logic inside WSL2 so the
REM install path is identical across OSes (single source of truth).
REM
REM Default profile: lite (no GPU needed on Windows).
REM ===========================================================================
setlocal enabledelayedexpansion
set "DIST_DIR=%~dp0"

echo.
echo ====================================================
echo  afmm_chat (Lite) installer for Windows
echo ====================================================
echo.

REM ---- 1. Docker Desktop running? -------------------------------------------
echo [1/4] Checking Docker Desktop...
where docker >nul 2>&1
if errorlevel 1 (
  echo   [FAIL] 'docker' not found on PATH.
  echo          Install Docker Desktop and enable the WSL2 backend:
  echo          https://www.docker.com/products/docker-desktop/
  goto :fail
)
docker version >nul 2>&1
if errorlevel 1 (
  echo   [FAIL] Docker is installed but the engine is not running.
  echo          Start Docker Desktop and wait until it says "Engine running",
  echo          then re-run install.bat.
  goto :fail
)
echo   [ OK ] Docker Desktop engine reachable.

REM ---- 2. WSL2 present? ------------------------------------------------------
echo [2/4] Checking WSL2...
where wsl >nul 2>&1
if errorlevel 1 (
  echo   [FAIL] WSL not found. Install it from an elevated PowerShell:
  echo            wsl --install
  echo          Reboot, then re-run install.bat.
  goto :fail
)
wsl -l -v >nul 2>&1
if errorlevel 1 (
  echo   [WARN] Could not list WSL distros. Ensure a WSL2 distro is installed:
  echo            wsl --install -d Ubuntu
) else (
  echo   [ OK ] WSL available. Distros:
  wsl -l -v
)

REM ---- 3. Translate this dir to a WSL path -----------------------------------
echo [3/4] Locating project inside WSL...
REM Convert the Windows DIST_DIR to a WSL path via wslpath.
for /f "usebackq delims=" %%P in (`wsl wslpath -a "%DIST_DIR%" 2^>nul`) do set "WSL_DIST=%%P"
if "%WSL_DIST%"=="" (
  echo   [WARN] wslpath failed; falling back to running from the current WSL dir.
  set "WSL_DIST=."
)
echo   Project (WSL path): %WSL_DIST%

REM ---- 4. Run the shared install.sh inside WSL2 ------------------------------
echo [4/4] Running install.sh inside WSL2 (shared logic)...
echo.
wsl bash -lc "cd '%WSL_DIST%' && chmod +x install.sh scripts/*.sh 2>/dev/null; ./install.sh"
if errorlevel 1 (
  echo.
  echo   [FAIL] The Linux installer reported an error (see output above).
  goto :fail
)

echo.
echo ====================================================
echo  Install finished. Open http://localhost:5013
echo  (or your APP_PORT) in a browser.
echo ====================================================
echo.
echo  NOTE (remote AF3): If AlphaFold3 runs on a REMOTE Linux host,
echo  set AF3_MCP_URL in configure.md to that reachable address. If the
echo  AF3 MCP binds 127.0.0.1 only, open an SSH tunnel first:
echo      ssh -L 8002:127.0.0.1:8002 user@af3-host
echo  then use  http://host.docker.internal:8002/mcp/  as AF3_MCP_URL.
echo  See docs\BRIDGE.md for details.
echo.
endlocal
exit /b 0

:fail
echo.
echo Installation aborted. Fix the issue above and re-run install.bat.
endlocal
exit /b 1
