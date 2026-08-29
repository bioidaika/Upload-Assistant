@echo off
cd /d "D:\Upload-Assistant"

py "D:\Upload-Assistant\upload.py" "%~1" -tk vmf,peergarden -ua --no-aka

pause