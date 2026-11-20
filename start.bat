@echo off
setlocal enabledelayedexpansion
title Telegram Vault
cd /d "%~dp0"

echo ==========================================
echo  Telegram Vault
echo ==========================================

if not exist ".env" (
    echo [HATA] .env bulunamadi.
    echo        .env.example dosyasini .env olarak kopyalayip doldur.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    echo [HATA] .venv bulunamadi. Once su komutu calistir:
    echo        py -3.12 -m venv .venv
    echo        .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "telegramlogs.db" if not defined DATABASE_URL (
    echo [BILGI] SQLite kullanilacak ^(telegramlogs.db^).
)

echo [1/2] Panel baslatiliyor - http://127.0.0.1:8000
start "Vault Panel" cmd /k "%PY% -m uvicorn app:app --host 127.0.0.1 --port 8000"

timeout /t 3 /nobreak >nul

echo [2/2] Telegram dinleyici baslatiliyor
start "Vault Bot" cmd /k "%PY% bot.py"

echo.
echo Panel : http://127.0.0.1:8000
echo Bot   : ayri pencerede calisiyor, kapatmak icin o pencereyi kapat.
echo.
echo Not: bot.py ayni session ile ikinci kez baslatilirsa kendini reddeder.
echo      Bu bilerek boyle - iki surec ayni session'a dokunursa Telegram
echo      auth key'i iptal eder.
echo ==========================================
timeout /t 5 /nobreak >nul
