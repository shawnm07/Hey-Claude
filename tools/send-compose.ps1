# Hey Claude helper: press Send on an open Gmail compose tab (Ctrl+Enter), like a person would.
# Guarded by the approval lock: it only runs if the user just gave a clear spoken "yes".
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File send-compose.ps1 -Subject "Lab report"
#
# Finds the browser window whose title shows a Gmail compose page ("Compose Mail") or the
# subject, brings it to the front, and presses Ctrl+Enter (Gmail's send shortcut).
param([string]$Subject = "")

Add-Type -Namespace HC -Name Win -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, System.IntPtr l);
public delegate bool EnumProc(System.IntPtr h, System.IntPtr l);
[DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(System.IntPtr h, System.Text.StringBuilder s, int n);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(System.IntPtr h);
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int c);
[DllImport("user32.dll")] public static extern bool IsIconic(System.IntPtr h);
[DllImport("user32.dll")] public static extern System.IntPtr GetForegroundWindow();
'@

$found = New-Object System.Collections.Generic.List[object]
$cb = [HC.Win+EnumProc]{
    param($h, $l)
    if ([HC.Win]::IsWindowVisible($h)) {
        $sb = New-Object System.Text.StringBuilder 512
        [void][HC.Win]::GetWindowText($h, $sb, 512)
        $t = $sb.ToString()
        if ($t -match 'Gmail|Compose|Mail' -and ($t -match 'Compose Mail' -or ($Subject -and $t -like "*$Subject*"))) {
            $found.Add([pscustomobject]@{ Handle = $h; Title = $t })
        }
    }
    return $true
}
[void][HC.Win]::EnumWindows($cb, [IntPtr]::Zero)

if ($found.Count -eq 0) {
    "NOT SENT: couldn't find a browser window showing the Gmail compose tab. Make sure the compose tab is the active tab in its window."
    exit 1
}
if ($found.Count -gt 1) {
    "NOT SENT: more than one compose window is open; ask the user which one (titles: " + (($found | ForEach-Object Title) -join ' | ') + ")."
    exit 1
}

$w = $found[0]
if ([HC.Win]::IsIconic($w.Handle)) { [void][HC.Win]::ShowWindow($w.Handle, 9) }
[void][HC.Win]::SetForegroundWindow($w.Handle)
Start-Sleep -Milliseconds 400
if ([HC.Win]::GetForegroundWindow() -ne $w.Handle) {
    "NOT SENT: couldn't bring the compose window to the front."
    exit 1
}
(New-Object -ComObject WScript.Shell).SendKeys("^{ENTER}")
Start-Sleep -Milliseconds 1500
"Pressed Send (Ctrl+Enter) in: $($w.Title). Take a screenshot to confirm it says 'Message sent'."
