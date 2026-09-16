@echo off
cd /d "%~dp0"
py -3 benchmark.py
if errorlevel 1 pause
