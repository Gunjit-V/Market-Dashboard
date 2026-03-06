@echo off
cd D:\code\HereWeGoAgain
call venv\Scripts\activate
python -m scheduler.run_download
deactivate