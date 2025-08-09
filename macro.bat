@echo off
cd /d "C:\Users\andre\dev\MailAutomation\cobol_support_agent"
pip install -r requirements.txt
call .venv\Scripts\activate
python app.py
pause
