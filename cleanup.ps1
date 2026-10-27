# Stop the dictation app (dictate.py), including the pythonw launcher.
$found = $false
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" | ForEach-Object {
    if ($_.CommandLine -and $_.CommandLine -like '*dictate.py*') {
        Stop-Process -Id $_.ProcessId -Force
        Write-Output "killed $($_.ProcessId): $($_.CommandLine)"
        $found = $true
    }
}
if (-not $found) { Write-Output "no dictation process" }
Get-CimInstance Win32_Process -Filter "Name='notepad.exe'" | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Write-Output "closed notepad $($_.ProcessId)"
}
