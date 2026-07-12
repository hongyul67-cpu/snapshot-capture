@echo off
REM SnapShot 재빌드 스크립트 (코드 수정 후 실행하면 dist\SnapShot.exe 새로 생성)
cd /d "%~dp0"
call .venv\Scripts\activate.bat
pyinstaller --noconfirm --onefile --windowed --name SnapShot ^
  --collect-binaries imageio_ffmpeg --collect-data imageio_ffmpeg ^
  capture_tool.py
echo.
echo 완료: dist\SnapShot.exe
pause
