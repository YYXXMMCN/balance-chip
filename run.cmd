@echo off
REM Launch the balance chip without a console window
set DIR=%~dp0
start "" "%DIR%.venv\Scripts\pythonw.exe" "%DIR%balance_chip.py"
