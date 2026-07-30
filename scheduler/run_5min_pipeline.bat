@echo off
cd /d "%~dp0.."
python -m scheduler.run_5min_pipeline
