@echo off
rem builds dist\Save Porter.exe with adb bundled in
rem needs: pip install pyinstaller pillow, and platform-tools\ next to this file

if not exist platform-tools\adb.exe (
    echo platform-tools\adb.exe not found. Download it from
    echo https://developer.android.com/tools/releases/platform-tools
    exit /b 1
)

python -m PyInstaller --noconfirm --onefile --windowed --name "Save Porter" ^
    --icon icon.ico --add-data "icon.ico;." ^
    --add-binary "platform-tools\adb.exe;." ^
    --add-binary "platform-tools\AdbWinApi.dll;." ^
    --add-binary "platform-tools\AdbWinUsbApi.dll;." ^
    gui.py
