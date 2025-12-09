@echo off
REM =============================================================================
REM Military Hierarchy Voice Relay System - All-in-One Installer & Launcher
REM =============================================================================
REM This script handles EVERYTHING:
REM   - Creates project folder
REM   - Creates virtual environment
REM   - Installs all dependencies
REM   - Launches the GUI
REM
REM Just double-click this file to get started!
REM =============================================================================

setlocal EnableDelayedExpansion

REM Configuration
set PROJECT_DIR=C:\PythonProjects\VoiceRelaySystem
set VENV_DIR=%PROJECT_DIR%\VoiceRelaySystem
set PYTHON=%VENV_DIR%\Scripts\python.exe
set PIP=%VENV_DIR%\Scripts\pip.exe

REM Colors via powershell (for better output)
set "GREEN=[92m"
set "RED=[91m"
set "YELLOW=[93m"
set "CYAN=[96m"
set "RESET=[0m"

cls
echo.
echo %CYAN%======================================================================%RESET%
echo %CYAN%   MILITARY HIERARCHY VOICE RELAY SYSTEM - INSTALLER%RESET%
echo %CYAN%======================================================================%RESET%
echo.

REM -----------------------------------------------------------------------------
REM STEP 1: Check Python
REM -----------------------------------------------------------------------------
echo %YELLOW%[STEP 1/6]%RESET% Checking Python installation...

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo %RED%ERROR: Python is not installed or not in PATH!%RESET%
    echo.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo %YELLOW%IMPORTANT: Check "Add Python to PATH" during installation!%RESET%
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo           %GREEN%Python %PYVER% found%RESET%

REM -----------------------------------------------------------------------------
REM STEP 2: Check FFmpeg
REM -----------------------------------------------------------------------------
echo.
echo %YELLOW%[STEP 2/6]%RESET% Checking FFmpeg installation...

ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo           %RED%FFmpeg NOT FOUND%RESET%
    echo.
    echo %YELLOW%WARNING: FFmpeg is REQUIRED for audio processing!%RESET%
    echo.
    echo   Download from: https://www.gyan.dev/ffmpeg/builds/
    echo   1. Download "ffmpeg-release-essentials.zip"
    echo   2. Extract to C:\ffmpeg
    echo   3. Add C:\ffmpeg\bin to your PATH variable
    echo   4. Restart this installer
    echo.
    set /p CONTINUE="Continue without FFmpeg? (y/n): "
    if /i not "!CONTINUE!"=="y" (
        echo.
        echo Installation cancelled. Install FFmpeg and try again.
        pause
        exit /b 1
    )
) else (
    echo           %GREEN%FFmpeg found%RESET%
)

REM -----------------------------------------------------------------------------
REM STEP 3: Create Project Directory
REM -----------------------------------------------------------------------------
echo.
echo %YELLOW%[STEP 3/6]%RESET% Setting up project directory...

if not exist "%PROJECT_DIR%" (
    mkdir "%PROJECT_DIR%"
    echo           %GREEN%Created %PROJECT_DIR%%RESET%
) else (
    echo           %GREEN%Project directory exists%RESET%
)

REM Copy files to project directory if running from different location
set "SCRIPT_DIR=%~dp0"
if /i not "%SCRIPT_DIR%"=="%PROJECT_DIR%\" (
    echo           Copying project files...
    copy /Y "%SCRIPT_DIR%*.py" "%PROJECT_DIR%\" >nul 2>&1
    copy /Y "%SCRIPT_DIR%*.txt" "%PROJECT_DIR%\" >nul 2>&1
    copy /Y "%SCRIPT_DIR%*.bat" "%PROJECT_DIR%\" >nul 2>&1
    copy /Y "%SCRIPT_DIR%*.json" "%PROJECT_DIR%\" >nul 2>&1
    copy /Y "%SCRIPT_DIR%*.md" "%PROJECT_DIR%\" >nul 2>&1
)

cd /d "%PROJECT_DIR%"

REM -----------------------------------------------------------------------------
REM STEP 4: Create Virtual Environment
REM -----------------------------------------------------------------------------
echo.
echo %YELLOW%[STEP 4/6]%RESET% Creating virtual environment...

if exist "%VENV_DIR%" (
    echo           %YELLOW%Virtual environment exists, skipping...%RESET%
) else (
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo.
        echo %RED%ERROR: Failed to create virtual environment!%RESET%
        pause
        exit /b 1
    )
    echo           %GREEN%Virtual environment created%RESET%
)

REM -----------------------------------------------------------------------------
REM STEP 5: Install Dependencies
REM -----------------------------------------------------------------------------
echo.
echo %YELLOW%[STEP 5/6]%RESET% Installing dependencies...
echo           This may take 1-2 minutes...
echo.

"%PYTHON%" -m pip install --upgrade pip --quiet 2>nul

"%PIP%" install discord.py[voice]>=2.3.2 --quiet
if errorlevel 1 (
    echo           %RED%Failed to install discord.py%RESET%
    pause
    exit /b 1
)
echo           %GREEN%discord.py installed%RESET%

"%PIP%" install discord-ext-voice-recv>=0.5.0a167 --quiet
if errorlevel 1 (
    echo           %RED%Failed to install discord-ext-voice-recv%RESET%
    pause
    exit /b 1
)
echo           %GREEN%discord-ext-voice-recv installed%RESET%

"%PIP%" install PyNaCl>=1.5.0 --quiet
echo           %GREEN%PyNaCl installed%RESET%

REM -----------------------------------------------------------------------------
REM STEP 6: Create Default Config
REM -----------------------------------------------------------------------------
echo.
echo %YELLOW%[STEP 6/6]%RESET% Checking configuration...

if not exist "%PROJECT_DIR%\config.json" (
    echo           Creating default config.json...
    (
        echo {
        echo     "commander_token": "",
        echo     "drone_alpha_token": "",
        echo     "drone_bravo_token": "",
        echo     "commander_user_id": "",
        echo     "drone_alpha_channel_id": "",
        echo     "drone_bravo_channel_id": "",
        echo     "command_prefix": "!",
        echo     "max_buffer_frames": 25,
        echo     "log_level": "DEBUG"
        echo }
    ) > "%PROJECT_DIR%\config.json"
    echo           %GREEN%Default config.json created%RESET%
) else (
    echo           %GREEN%config.json exists%RESET%
)

REM -----------------------------------------------------------------------------
REM COMPLETE - Launch GUI
REM -----------------------------------------------------------------------------
echo.
echo %GREEN%======================================================================%RESET%
echo %GREEN%   INSTALLATION COMPLETE!%RESET%
echo %GREEN%======================================================================%RESET%
echo.
echo   Project Location: %PROJECT_DIR%
echo   Virtual Env:      %VENV_DIR%
echo.
echo   Launching GUI...
echo.

cd /d "%PROJECT_DIR%"
"%PYTHON%" gui.py

if errorlevel 1 (
    echo.
    echo %RED%GUI failed to start. Running in console mode...%RESET%
    echo.
    pause
)

exit /b 0
