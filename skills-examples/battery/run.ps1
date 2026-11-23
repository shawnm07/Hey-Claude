# Hey Claude skill: battery level and charging state, as one spoken sentence.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$p = [System.Windows.Forms.SystemInformation]::PowerStatus
if ($p.BatteryChargeStatus -match 'NoSystemBattery') { "This computer has no battery."; exit 0 }
$pct = [math]::Round($p.BatteryLifePercent * 100)
if ($p.PowerLineStatus -eq 'Online') {
    $state = if ($p.BatteryChargeStatus -match 'Charging') { 'and charging' } else { 'and plugged in' }
    "Battery is at $pct percent $state."
} else {
    $left = if ($p.BatteryLifeRemaining -gt 0) { ", about $([math]::Round($p.BatteryLifeRemaining / 3600, 1)) hours left" } else { '' }
    "Battery is at $pct percent, on battery$left."
}