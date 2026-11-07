@echo off
setlocal
pushd "%~dp0"
if exist "%~dp0LP2Bridge.exe" (
  start "LP2 Bridge" "%~dp0LP2Bridge.exe" --transport bluetooth %*
) else if exist "%~dp0dist\LP2Bridge.exe" (
  start "LP2 Bridge" "%~dp0dist\LP2Bridge.exe" --transport bluetooth %*
) else if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -m bridge --transport bluetooth %*
) else (
  python -m bridge --transport bluetooth %*
)
set "LP2_BRIDGE_EXIT=%ERRORLEVEL%"
popd
exit /b %LP2_BRIDGE_EXIT%
