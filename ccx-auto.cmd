@echo off
rem Sobe o monitor de cota do Claude Code. Deixe esta janela aberta enquanto trabalha.
cd /d "%~dp0"
python ccx.py auto %*
pause
