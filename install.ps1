# Dictation installer for Windows (ASCII only: PowerShell 5.1 reads BOM-less UTF-8 as ANSI).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $python)) {
    Write-Host "Creating venv..."
    & "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe" -m venv (Join-Path $root ".venv")
}

Write-Host "Installing deps..."
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $root "requirements.txt")

$vbs = Join-Path $root "start-dictate.vbs"
$script = Join-Path $root "dictate.py"
# Строки собираем ДО @(): в PS 5.1 внутри массивного литерала
# `'a' + $var + 'b'` распадается на отдельные элементы.
$line1 = 'Set sh = CreateObject("WScript.Shell")'
$line2 = 'sh.CurrentDirectory = "' + $root + '"'
$line3 = 'sh.Run """' + $pythonw + '"" ""' + $script + '""", 0, False'
$lines = @($line1, $line2, $line3)
Set-Content -Path $vbs -Value $lines -Encoding ASCII

$startup = [Environment]::GetFolderPath("Startup")
Copy-Item -Path $vbs -Destination (Join-Path $startup "dictate.vbs") -Force
Write-Host "Autostart: $(Join-Path $startup 'dictate.vbs')"

Get-Process pythonw -ErrorAction SilentlyContinue | Where-Object {
    $_.Path -eq $pythonw
} | Stop-Process -Force
Start-Process -FilePath "wscript.exe" -ArgumentList "`"$vbs`""
Write-Host "Dictation started. Hotkeys: see config.json"
