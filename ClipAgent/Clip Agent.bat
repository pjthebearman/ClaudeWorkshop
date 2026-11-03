@echo off
rem Starts the Clip Agent. Keep this window open while you play.
title Clip Agent
where py >nul 2>nul
if %errorlevel%==0 (py "%~dp0clip_agent.py" %*) else (python "%~dp0clip_agent.py" %*)
if errorlevel 1 pause
