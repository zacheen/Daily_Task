<#
Fires a native Windows toast. Shared by every scheduled task in this repo.

Exists because Claude's PushNotification tool suppresses itself whenever it
thinks the user is present, which includes the case where they are looking at a
different session and cannot see the scheduled run's output at all. This path
has no such suppression.

Toasts are attributed to the PowerShell AppId because a toast requires an AppId
already registered with Windows; an unregistered one is silently dropped.

MUST stay UTF-8 WITH BOM. Windows PowerShell 5.1 decodes a BOM-less .ps1 as
system ANSI (CP950 here), which mangles the $Title default into garbage while
command-line -Message still renders fine, so the damage looks like a caller bug.
Most editors and file-writing tools drop the BOM; re-add it after any rewrite.
#>
param(
    [Parameter(Mandatory = $true)][string]$Message,
    [string]$Title = "每日排程"
)

$ErrorActionPreference = "Stop"

$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null

# Message text is model-generated and may contain & or angle brackets.
function Convert-ToXmlText([string]$s) {
    $s.Replace('&', '&amp;').Replace('<', '&lt;').Replace('>', '&gt;')
}

$xml = @"
<toast scenario="reminder">
  <visual>
    <binding template="ToastGeneric">
      <text>$(Convert-ToXmlText $Title)</text>
      <text>$(Convert-ToXmlText $Message)</text>
    </binding>
  </visual>
  <audio src="ms-winsoundevent:Notification.Default" />
</toast>
"@

$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)

$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)

Write-Output "Toast sent."
