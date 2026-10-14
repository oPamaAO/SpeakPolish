param(
  [string]$Target="",
  [string]$Args="",
  [string]$WorkDir="",
  [string]$Link="",
  [string]$DesktopLink="",
  [string]$StartupLink="",
  [int]$WindowStyle=1,
  [string]$Desc=""
)
# Desktop/Startup resolved via .NET - honors OneDrive moves and any other
# redirections (hardcoded %USERPROFILE%\Desktop lies in that case).
if ($DesktopLink -ne "") {
  $Link = Join-Path ([Environment]::GetFolderPath("Desktop")) $DesktopLink
}
if ($StartupLink -ne "") {
  $Link = Join-Path ([Environment]::GetFolderPath("Startup")) $StartupLink
}
if ($Link -eq "" -or $Target -eq "") { Write-Error "Need Link (or DesktopLink/StartupLink) + Target"; exit 1 }
try {
  $dir = Split-Path $Link -Parent
  if (-not (Test-Path $dir)) { throw "Folder not found: $dir" }
  $ws = New-Object -ComObject WScript.Shell
  $lnk = $ws.CreateShortcut($Link)
  $lnk.TargetPath = $Target
  if ($Args -ne "") { $lnk.Arguments = $Args }
  if ($WorkDir -ne "") { $lnk.WorkingDirectory = $WorkDir }
  $lnk.WindowStyle = $WindowStyle
  if ($Desc -ne "") { $lnk.Description = $Desc }
  $lnk.Save()
  if (-not (Test-Path $Link)) { throw "Save reported OK but file missing" }
  Write-Output "LINK_OK $Link"
} catch {
  Write-Error "LINK_FAIL: $_"
  exit 1
}
