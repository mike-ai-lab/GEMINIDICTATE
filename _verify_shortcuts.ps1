$ws = New-Object -ComObject WScript.Shell
$d  = [System.Environment]::GetFolderPath('Desktop')
$p  = [System.Environment]::GetFolderPath('Programs')

$paths = @(
    @{ label = "DESKTOP";    path = "$d\GEMINIDICTATE.lnk" },
    @{ label = "START MENU"; path = "$p\GEMINIDICTATE.lnk" }
)

foreach ($entry in $paths) {
    $label = $entry.label
    $lnkPath = $entry.path
    if (-not (Test-Path $lnkPath)) {
        Write-Host "$label : MISSING ($lnkPath)"
        continue
    }
    $lnk = $ws.CreateShortcut($lnkPath)
    $argFile = $lnk.Arguments.Trim('"')
    $vbsOk   = Test-Path $argFile
    $iconOk  = ($lnk.IconLocation -ne '') -and (Test-Path ($lnk.IconLocation.Split(',')[0]))
    Write-Host "=== $label ==="
    Write-Host "  File       : $lnkPath"
    Write-Host "  Target     : $($lnk.TargetPath)"
    Write-Host "  Arguments  : $($lnk.Arguments)"
    Write-Host "  WorkingDir : $($lnk.WorkingDirectory)"
    Write-Host "  Icon       : $($lnk.IconLocation)"
    Write-Host "  WindowStyle: $($lnk.WindowStyle)"
    Write-Host "  VBS exists : $vbsOk"
    Write-Host "  ICO exists : $iconOk"
}
