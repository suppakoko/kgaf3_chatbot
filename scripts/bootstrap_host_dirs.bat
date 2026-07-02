@echo off
REM bootstrap_host_dirs.bat - Windows host-dir bootstrap for afmm_chat.
REM Named-volume data lives in Docker volumes; only AF3_OUTPUT_ROOT must exist.
REM Reads AF3_OUTPUT_ROOT from the environment or from ..\.env.docker.
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%.." >nul
set "DIST_DIR=%CD%"
popd >nul
set "ENV_FILE=%DIST_DIR%\.env.docker"

if "%AF3_OUTPUT_ROOT%"=="" if exist "%ENV_FILE%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    if /i "%%A"=="AF3_OUTPUT_ROOT" set "AF3_OUTPUT_ROOT=%%B"
  )
)

echo [bootstrap] DIST=%DIST_DIR%

set "LOCAL_DATA=%DIST_DIR%\data"
if not exist "%LOCAL_DATA%" mkdir "%LOCAL_DATA%"
echo [bootstrap] ensured %LOCAL_DATA%

if "%AF3_OUTPUT_ROOT%"=="" (
  echo [bootstrap] ERROR: AF3_OUTPUT_ROOT is not set ^(env or .env.docker^). 1>&2
  exit /b 2
)
if not exist "%AF3_OUTPUT_ROOT%" (
  echo [bootstrap] ERROR: AF3_OUTPUT_ROOT does not exist: %AF3_OUTPUT_ROOT% 1>&2
  echo [bootstrap]        Create it or point it at your AF3 output directory. 1>&2
  exit /b 2
)
echo [bootstrap] AF3_OUTPUT_ROOT OK: %AF3_OUTPUT_ROOT%
echo [bootstrap] done.
endlocal
