@echo off
title Painel Recebimento PA — BURN RJ
cd /d "%~dp0"
echo.
echo  ================================================
echo   Gerenciamento de Rotina Logistica BURN RJ
echo   Painel de Recebimento Produto Acabado
echo  ================================================
echo.
echo  Instalando dependencias...
pip install -r requirements.txt -q
echo.
echo  Iniciando servidor na porta 5002...
echo  Acesse: http://localhost:5002
echo.
python app.py
pause
