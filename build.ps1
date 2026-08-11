# Builds dist\MeglaPing.exe
# The .tcss stylesheet is a data file, so PyInstaller has to be told about it, and
# textual ships its own assets that --collect-all picks up.

Get-Process MeglaPing -ErrorAction SilentlyContinue | Stop-Process -Force

python -m PyInstaller --onefile --name MeglaPing --console --clean `
    --icon assets/meglaping.ico `
    --add-data "meglaping/app.tcss;meglaping" `
    --collect-all textual `
    --exclude-module pytest --exclude-module ruff `
    main.py

if ($LASTEXITCODE -eq 0) { "`nBuilt dist\MeglaPing.exe" }
