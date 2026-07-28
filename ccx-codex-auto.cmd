@echo off
rem Sobe o monitor de cota do Codex CLI. Deixe esta janela aberta enquanto trabalha.
cd /d "%~dp0"
python ccx_codex.py auto %*
pause
