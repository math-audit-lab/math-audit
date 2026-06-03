@echo off
setlocal EnableExtensions

REM Double-clickable Windows launcher for Math Paper Audit.
REM It creates/uses the conda environment, runs the setup check, then starts the GUI.

set "ENV_NAME=math-audit"
set "CONDA_TOOL="

cd /d "%~dp0" || goto :repo_error

call :find_env_tool
if not defined CONDA_TOOL goto :missing_conda

if not exist "environment.yml" (
  echo.
  echo ERROR: environment.yml was not found.
  echo Make sure this launcher is inside the Math Paper Audit folder.
  goto :fail
)

echo Math Paper Audit launcher
echo Project folder: %CD%
echo Environment tool: %CONDA_TOOL%
echo.

call :env_exists
if errorlevel 1 (
  echo Creating "%ENV_NAME%" environment from environment.yml.
  echo This may take several minutes the first time.
  echo.
  call "%CONDA_TOOL%" env create -f environment.yml
  if errorlevel 1 goto :env_create_failed
) else (
  echo Using existing "%ENV_NAME%" environment.
)

echo.
echo Running setup check...
call "%CONDA_TOOL%" run -n "%ENV_NAME%" python scripts\check_setup.py
if errorlevel 1 (
  echo.
  echo ERROR: Setup check failed. See the messages above and QUICKSTART.md.
  goto :fail
)

echo.
echo Launching Math Paper Audit GUI...
echo Paste your OpenAI API key in the GUI when you are ready to use live audit/discussion actions.
echo.
call "%CONDA_TOOL%" run -n "%ENV_NAME%" python audit_gui.py
if errorlevel 1 (
  echo.
  echo ERROR: The GUI exited with an error.
  goto :fail
)

echo.
echo Math Paper Audit has closed. You can close this Command Prompt window.
goto :eof

:find_env_tool
for /f "delims=" %%I in ('where conda 2^>nul') do (
  set "CONDA_TOOL=%%I"
  goto :eof
)
for /f "delims=" %%I in ('where mamba 2^>nul') do (
  set "CONDA_TOOL=%%I"
  goto :eof
)

for %%I in (
  "%USERPROFILE%\miniforge3\Scripts\conda.exe"
  "%USERPROFILE%\miniforge3\condabin\conda.bat"
  "%USERPROFILE%\miniconda3\Scripts\conda.exe"
  "%USERPROFILE%\miniconda3\condabin\conda.bat"
  "%USERPROFILE%\anaconda3\Scripts\conda.exe"
  "%USERPROFILE%\anaconda3\condabin\conda.bat"
  "%USERPROFILE%\mambaforge\Scripts\mamba.exe"
  "%USERPROFILE%\mambaforge\condabin\mamba.bat"
) do (
  if exist "%%~I" (
    set "CONDA_TOOL=%%~I"
    goto :eof
  )
)
goto :eof

:env_exists
call "%CONDA_TOOL%" env list | findstr /R /C:"^%ENV_NAME%[ ]" /C:"^%ENV_NAME%$" >nul
exit /b %ERRORLEVEL%

:missing_conda
echo Math Paper Audit needs Miniforge, Conda, or Mamba to create its Python environment.
echo.
echo Recommended Windows installer:
echo   https://github.com/conda-forge/miniforge/releases
echo.
echo Install Miniforge, then double-click run_math_audit.bat again.
goto :fail

:env_create_failed
echo.
echo Environment creation failed.
echo.
echo Common causes:
echo   - Miniforge/Conda installation is incomplete.
echo   - Internet access is unavailable.
echo   - The environment already exists but is damaged.
echo   - Package downloads were interrupted.
echo.
echo See QUICKSTART.md for manual setup instructions.
goto :fail

:repo_error
echo ERROR: Could not open the Math Paper Audit folder.
goto :fail

:fail
echo.
echo Press any key to close this window.
pause >nul
exit /b 1
