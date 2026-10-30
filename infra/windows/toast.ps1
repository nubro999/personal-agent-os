# Windows 알림 하나를 띄운다. 브리핑(pao-briefing)이 부른다.
# 일반 토스트는 몇 초 뒤 알림 센터로 조용히 들어가서 놓치기 쉽다. reminder는 닫을 때까지 화면에 남는다.
# Windows PowerShell은 BOM 없는 스크립트를 UTF-8로 읽지 않는다. 그래서 버튼 글자는 XML 문자 참조로 쓴다.
param([string]$Title, [string]$Body)
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null
$t = [System.Security.SecurityElement]::Escape($Title)
$b = [System.Security.SecurityElement]::Escape($Body)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast scenario='reminder'><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual><actions><action content='&#xB2EB;&#xAE30;' arguments='dismiss' activationType='system'/></actions></toast>")
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
