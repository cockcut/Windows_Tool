@echo off
setlocal EnableExtensions
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python is not in PATH. Install Python 3.10+ first.
  pause
  exit /b 1
)

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo Python 3.10+ is required.
  pause
  exit /b 1
)

echo [1/3] Install PyInstaller
python -m pip install --upgrade pip pyinstaller
if errorlevel 1 (
  echo pip / PyInstaller install failed
  pause
  exit /b 1
)

echo [2/3] Build exe into dist\
if exist build rd /s /q build
if exist dist rd /s /q dist
if exist Connect-Bssid-GUI.spec del /q Connect-Bssid-GUI.spec
python -m PyInstaller --noconfirm --clean --windowed --onefile --name Connect-Bssid-GUI --distpath dist --workpath build --specpath build Connect-Bssid-GUI.py
if errorlevel 1 (
  echo Build failed
  pause
  exit /b 1
)

echo [3/3] Keep only dist\Connect-Bssid-GUI.exe
if exist build rd /s /q build
if exist Connect-Bssid-GUI.spec del /q Connect-Bssid-GUI.spec
if exist __pycache__ rd /s /q __pycache__
for /d %%D in (dist\*) do (
  if /I not "%%~nxD"=="Connect-Bssid-GUI.exe" rd /s /q "%%D"
)
for %%F in (dist\*) do (
  if /I not "%%~nxF"=="Connect-Bssid-GUI.exe" del /q "%%F"
)

if not exist "dist\Connect-Bssid-GUI.exe" (
  echo dist\Connect-Bssid-GUI.exe not found
  pause
  exit /b 1
)

echo.
echo Done: %cd%\dist\Connect-Bssid-GUI.exe
pause
endlocal
