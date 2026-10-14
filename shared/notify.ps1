<#
Fires a native Windows toast. Shared by every scheduled task in this repo and by other projects on this machine.

Exists because Claude's PushNotification tool suppresses itself whenever it
thinks the user is present, which includes the case where they are looking at a
different session and cannot see the scheduled run's output at all. This path
has no such suppression.

Each -Category sends under its own AppId, so every project is its own app in the
notification centre and in Settings > System > Notifications, where it can be
switched off alone. Windows accepts toasts from an unpackaged AppId only when a
Start menu shortcut carries that id, so the first toast of a category creates
"<Category>.lnk" under the user's Start menu Programs folder. A registry key
alone was tried and its toasts were dropped.
The PowerShell AppId used before is not a fallback. Windows' notification
suggestion once switched it off, which hides it from Settings, so it could not
be switched back on.

Keep it UTF-8 WITH BOM. Windows PowerShell 5.1 decodes a BOM-less .ps1 as
system ANSI (CP950 here), so any non-ASCII literal added later turns to garbage
while command-line -Message still renders fine, which looks like a caller bug.
Most editors and file-writing tools drop the BOM. Re-add it after any rewrite.

The $Title and $Category defaults are English because a fixed string cannot
follow the report language, and neutral because the repo is public and must not
reveal what a task does. Callers supply a title in that language unless it is a
fixed name. A caller that passes no -Category lands in "Daily tasks".
#>
param(
    [Parameter(Mandatory = $true)][string]$Message,
    [string]$Title = "Daily tasks",
    [string]$Category = "Daily tasks"
)

$ErrorActionPreference = "Stop"

$appId = "ClaudeCode." + ($Category -replace '[^A-Za-z0-9]', '')
$name = $Category -replace '[\\/:*?"<>|]', ''
$lnk = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$name.lnk"

if (-not (Test-Path $lnk)) {
    Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
public static class AumidShortcut {
  [ComImport, Guid("00021401-0000-0000-C000-000000000046")] class CShellLink {}
  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("000214F9-0000-0000-C000-000000000046")]
  interface IShellLinkW {
    void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder f, int c, IntPtr fd, uint fl);
    void GetIDList(out IntPtr p);
    void SetIDList(IntPtr p);
    void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder n, int c);
    void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string n);
    void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder d, int c);
    void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string d);
    void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder a, int c);
    void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string a);
    void GetHotkey(out short h);
    void SetHotkey(short h);
    void GetShowCmd(out int s);
    void SetShowCmd(int s);
    void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder p, int c, out int i);
    void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string p, int i);
    void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string p, uint r);
    void Resolve(IntPtr h, uint f);
    void SetPath([MarshalAs(UnmanagedType.LPWStr)] string f);
  }
  [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown), Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")]
  interface IPropertyStore {
    void GetCount(out uint c);
    void GetAt(uint i, out PROPERTYKEY k);
    void GetValue(ref PROPERTYKEY k, out PROPVARIANT v);
    void SetValue(ref PROPERTYKEY k, ref PROPVARIANT v);
    void Commit();
  }
  [StructLayout(LayoutKind.Sequential, Pack = 4)] public struct PROPERTYKEY { public Guid fmtid; public uint pid; }
  [StructLayout(LayoutKind.Explicit)] public struct PROPVARIANT { [FieldOffset(0)] public ushort vt; [FieldOffset(8)] public IntPtr p; }

  public static void Create(string lnk, string target, string args, string aumid) {
    var link = (IShellLinkW)new CShellLink();
    link.SetPath(target);
    link.SetArguments(args);
    var key = new PROPERTYKEY { fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), pid = 5 };  // PKEY_AppUserModel_ID
    var v = new PROPVARIANT { vt = 31, p = Marshal.StringToCoTaskMemUni(aumid) };  // VT_LPWSTR
    var store = (IPropertyStore)link;
    store.SetValue(ref key, ref v);
    store.Commit();
    ((IPersistFile)link).Save(lnk, true);
    Marshal.FreeCoTaskMem(v.p);
  }
}
"@
    # The shortcut only has to exist. Opening it from the Start menu starts a PowerShell that exits at once.
    [AumidShortcut]::Create($lnk, "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile -Command exit", $appId)
    Start-Sleep -Seconds 3  # the Start menu indexes a new shortcut asynchronously, and a toast sent before that is dropped
}

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
