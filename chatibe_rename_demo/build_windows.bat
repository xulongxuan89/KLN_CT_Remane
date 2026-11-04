@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv-build\Scripts\python.exe" (
    py -3 -m venv .venv-build
    if errorlevel 1 goto :error
)

call ".venv-build\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 goto :error
python -m pip install -r requirements.txt -r requirements-build.txt
if errorlevel 1 goto :error

pyinstaller --noconfirm --clean rename_app.spec
if errorlevel 1 goto :error

echo.
echo Build complete:
echo %CD%\dist\CHATIBE_Rename_Tool
echo.
echo Zip this directory and distribute it through the company software portal
echo or approved intranet location. End users do not need Python installed.
exit /b 0

:error
echo.
echo Build failed.
exit /b 1
