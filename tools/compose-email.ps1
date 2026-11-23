# Hey Claude helper: open a NEW Gmail compose tab in the default browser, pre-filled.
# Nothing is sent. The user (or send-compose.ps1, after spoken approval) presses Send.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File compose-email.ps1 -To "a@b.com" -Subject "Hi" -Body "Line 1`nLine 2"
param(
    [string]$To = "",
    [string]$Cc = "",
    [string]$Bcc = "",
    [string]$Subject = "",
    [string]$Body = "",
    [switch]$PrintOnly          # testing: show the link instead of opening it
)

$q = @("view=cm", "fs=1")
if ($To)      { $q += "to="  + [uri]::EscapeDataString($To) }
if ($Cc)      { $q += "cc="  + [uri]::EscapeDataString($Cc) }
if ($Bcc)     { $q += "bcc=" + [uri]::EscapeDataString($Bcc) }
if ($Subject) { $q += "su="  + [uri]::EscapeDataString($Subject) }
if ($Body)    { $q += "body=" + [uri]::EscapeDataString(($Body -replace '\\n', "`n")) }

$url = "https://mail.google.com/mail/?" + ($q -join "&")
if ($PrintOnly) { $url; exit 0 }
Start-Process $url
"Opened a Gmail compose tab in the browser (NOT sent). To: $To | Subject: $Subject"
