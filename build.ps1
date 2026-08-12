# Builds dist\MeglaPing.exe
# The .tcss stylesheet is a data file, so PyInstaller has to be told about it, and
# textual ships its own assets that --collect-all picks up.

# A running MeglaPing holds dist\MeglaPing.exe open and the build fails with
# "Access is denied", so it has to go. Say so rather than closing it silently.
$running = Get-Process MeglaPing -ErrorAction SilentlyContinue
if ($running) {
    "closing $($running.Count) running MeglaPing process(es) so the exe can be replaced"
    $running | Stop-Process -Force
    Start-Sleep -Milliseconds 300
}

python -m PyInstaller --onefile --name MeglaPing --console --clean `
    --icon assets/meglaping.ico `
    --add-data "meglaping/app.tcss;meglaping" `
    --collect-all textual `
    --exclude-module pytest --exclude-module ruff `
    main.py

if ($LASTEXITCODE -eq 0) { "`nBuilt dist\MeglaPing.exe" }
