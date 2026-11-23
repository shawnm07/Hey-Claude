# Hey Claude skill: set the built-in display's brightness (0-100). External monitors don't support this.
# Prints "UNDO: <previous level>" so "undo that" can put it back (that line is never shown or spoken).
param([int]$Level)
$ErrorActionPreference = 'Stop'
$prev = (Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -First 1).CurrentBrightness
$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | Select-Object -First 1
Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness -Arguments @{ Timeout = 1; Brightness = [byte]$Level } | Out-Null
"Brightness set to $Level percent."
"UNDO: $prev"