@echo off
chcp 65001 >nul
echo ===================================================
echo  Safe Market Leaders Quant Pipeline [WEEKLY]
echo ===================================================

:: 1. Virtual Environment Activation
if exist "venv\Scripts\activate.bat" (
    echo Activating virtual environment...
    call venv\Scripts\activate.bat
) else (
    echo Warning: venv environment not found. Using global python.
)

:: 2. Pipeline Run
echo Running weekly pipeline...
python main.py --schedule weekly
if %ERRORLEVEL% neq 0 (
    echo Error: Pipeline execution failed.
    exit /b 1
)

:: 3. Get Current Date (via PowerShell)
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd'"') do set TODAY=%%i
echo Current Date: %TODAY%

:: 4. Git Operations
echo Committing and pushing results to Git...
git add -A weekly_results/
git commit -m "%TODAY% output"
git push origin main

if %ERRORLEVEL% neq 0 (
    echo Warning: Git push failed. Please check authentication/settings.
) else (
    echo Git push completed successfully.
)

echo.
echo All processes finished.
