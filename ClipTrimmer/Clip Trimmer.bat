@echo off
rem Drag a video onto this file, or double-click to choose one.
where py >nul 2>nul
if %errorlevel%==0 (py "%~dp0clip_trimmer.py" %1) else (python "%~dp0clip_trimmer.py" %1)
if errorlevel 1 pause
