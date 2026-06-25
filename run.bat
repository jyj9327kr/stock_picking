@echo off
chcp 65001 >nul
echo ===================================================
echo  Safe Market Leaders Quant Pipeline [WEEKLY]
echo ===================================================

:: 1. 가상환경 활성화
if exist "venv\Scripts\activate.bat" (
    echo 가상환경 활성화 중...
    call venv\Scripts\activate.bat
) else (
    echo [경고] venv 가상환경을 찾을 수 없습니다. 글로벌 파이썬을 사용합니다.
)

:: 2. 파이프라인 실행
echo 파이프라인을 실행합니다 (주간 스크리닝 및 데이터 생성)...
python main.py --schedule weekly
if %ERRORLEVEL% neq 0 (
    echo [오류] 파이프라인 실행 중 오류가 발생했습니다.
    pause
    exit /b %ERRORLEVEL%
)

:: 3. 오늘 날짜 구하기 (PowerShell 이용)
for /f "tokens=*" %%i in ('powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd'"') do set TODAY=%%i
echo 오늘의 날짜: %TODAY%

:: 4. Git 작업 진행
echo Git 저장소에 결과를 커밋 및 푸시합니다...
git add -A weekly_results/
git commit -m "%TODAY% output"
git push origin main

if %ERRORLEVEL% neq 0 (
    echo [경고] Git 푸시 중 오류가 발생했습니다. SSH/HTTPS 인증 설정을 확인하세요.
) else (
    echo 성공적으로 Git push를 완료했습니다.
)

echo.
echo 모든 작업이 완료되었습니다.
:: pause
