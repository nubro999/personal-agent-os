# Windows 로그인 때 WSL(Ubuntu)을 깨우고 계속 켜 둔다. systemd가 Postgres와 에이전트를 띄운다.
# 창 없이 wsl.exe 하나를 붙잡아 두는 방식이라 WSL이 idle로 꺼지지 않는다.
# 해제: Unregister-ScheduledTask -TaskName PersonalAgentOS-WSL -Confirm:$false
$name = "PersonalAgentOS-WSL"
$action = New-ScheduledTaskAction -Execute "C:\Windows\System32\conhost.exe" `
  -Argument "--headless C:\Windows\System32\wsl.exe -d Ubuntu --exec /bin/sleep infinity"
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
  -Description "Keep WSL running for Personal Agent OS" -Force | Out-Null
Start-ScheduledTask -TaskName $name
Get-ScheduledTask -TaskName $name | Select-Object TaskName, State
