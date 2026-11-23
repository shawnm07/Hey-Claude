# Hey Claude skill: set the built-in display's brightness (0-100). External monitors don't support this.
param([int]$Level)
$ErrorActionPreference = 'Stop'
$m = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | Select-Object -First 1
Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness -Arguments @{ Timeout = 1; Brightness = [byte]$Level } | Out-Null
"Brightness set to $Level percent."