@echo off
setlocal
cd /d "%~dp0"
rem 수정된 .py 를 우선 실행한다. 예전 exe(0.1.6)가 있으면 패치가 무시된다.
where python >nul 2>&1
if not errorlevel 1 (
  if exist "%~dp0Connect-Bssid-GUI.py" (
    python "%~dp0Connect-Bssid-GUI.py"
    if errorlevel 1 pause
    goto :eof
  )
)
if exist "%~dp0dist\Connect-Bssid-GUI.exe" (
  start "" "%~dp0dist\Connect-Bssid-GUI.exe"
  goto :eof
)
if exist "%~dp0Connect-Bssid-GUI.exe" (
  start "" "%~dp0Connect-Bssid-GUI.exe"
  goto :eof
)
echo Connect-Bssid-GUI.py / exe not found, and Python is not in PATH.
echo Run build_exe.bat or install Python.
pause
endlocal