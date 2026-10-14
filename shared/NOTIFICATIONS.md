# Notifications

Everything about sending a Windows toast to the user lives here. Read this before calling `notify.ps1`, adding a project that sends toasts, or chasing a toast that did not arrive.

## How to send one

Every caller uses this form, run with the Bash tool.

```
powershell.exe -NoProfile -File "D:\dont_move\git_save\Daily_Task\shared\notify.ps1" -Message "<message>" -Title "<task name>" -Category "<project>"
```

- `-Message` comes first, `-Title` second and `-Category` last. The order does not affect permission matching, but keeping every caller the same makes them easier to maintain.
- The caller supplies `-Title`. The default is deliberately neutral, because the script lives in a public repo and its default should not reveal what any task does.
- `-Category` is optional. A caller that leaves it out lands in `Daily tasks`, which is where every scheduled task of this repo belongs.
- Put no double quotes in the message, because they break the quoting of the calling command. Leave colons out as well, since the fixed toast sentences of Email_Check are tested for both.
- Do not use the PowerShell call operator form `& "path" ...`. Even with a word-for-word matching rule in both settings files, a scheduled run hung on a permission prompt with no alert at all. The leading suspect is that the command parser treats `&` as a separator, so the prefix match never lines up. The `-File` form above has no `&` and was tested to work.
- The permission rules end in `*`, so adding `-Category` to a call still matches them.

## Why every project gets its own app

Windows groups toasts by the AppId that sends them. The script used to send everything under the PowerShell AppId, so toasts from every project merged into one stack and could not be told apart.

Each category now sends under `ClaudeCode.<category without spaces or symbols>`, for example `ClaudeCode.RSNAKnee` for `-Category "RSNA Knee"`. Every project therefore shows as its own app, both in the notification centre and in Settings, System, Notifications, where it can be switched off on its own.

Windows accepts toasts from an unpackaged AppId only when a Start menu shortcut carries that id. The first toast of a new category therefore creates `<category>.lnk` under `%APPDATA%\Microsoft\Windows\Start Menu\Programs`, with the AppId written into the shortcut's `System.AppUserModel.ID` property. Opening the shortcut only starts a PowerShell that exits at once. Deleting the shortcut removes the app again.

The categories in use on 2026-10-13 are `Daily tasks`, `RSNA Knee` and `Event Scout`.

## What did not work

- **A registry key alone.** A key under `HKCU\Software\Classes\AppUserModelId` with a display name was tried first. Windows accepted the call and dropped every toast without an error.
- **A header inside the PowerShell app.** Giving each toast a `<header>` element still sends everything under PowerShell, and it did nothing once PowerShell itself was switched off.
- **Switching PowerShell back on.** See the incident below.

## The 2026-10-13 incident

From 19:35 on 2026-10-13 no toast arrived, including the scheduled Gmail checks, and the script still printed `Toast sent.`.

- Windows' notification suggestion feature offers to switch off an app that sends many toasts the user rarely clicks. The PowerShell AppId had sent 127 toasts with 4 clicks, and the suggestion was accepted by mistake.
- A switched-off app disappears from the Settings list, so it could not be switched back on there, not even with the list sorted by name.
- Setting its `Enabled` value back to 1 under `HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings` changed nothing, not even after a reboot. Windows 11 keeps the real setting in `%LOCALAPPDATA%\Microsoft\Windows\Notifications\wpndatabase.db`, and the registry is only a copy. Do not edit that database by hand.
- Moving to per-category AppIds fixed it, because each new AppId starts switched on.

If the same suggestion appears for one of the `ClaudeCode` apps, dismiss it rather than accepting it.

## When a toast does not arrive

Check what Windows thinks of the AppId before suspecting the caller. `Toast sent.` only means the call returned, not that Windows showed anything.

```
powershell.exe -NoProfile -Command "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('ClaudeCode.Dailytasks').Setting"
```

- `Enabled` means Windows accepts toasts from it. Look next at Focus or Do not disturb.
- `DisabledForApplication` means that app was switched off. If it is listed in Settings, switch it on there. If it is not listed, delete its shortcut and send again, which registers it afresh.
- `History.GetHistory('<AppId>')` on the same class lists the toasts still in the notification centre, which shows whether one was actually delivered.

A toast Claude Code itself raises, attributed to the Claude app, is not from this script. Its text summarises the session's last reply, so it is easy to mistake for a delivered test toast.

## Not PushNotification

A scheduled task that needs to notify the user always goes through this script, never Claude's built-in PushNotification tool. PushNotification pushes to the phone, which needs Remote Control, and the organization this account belongs to has not granted that permission, so calling it only returns Mobile push not sent and sends nothing. It also suppresses itself when it decides the user is present, and the user is often looking at another session and never sees the scheduled output.

## Editing notify.ps1

- Keep it UTF-8 with BOM. It holds only ASCII now, but Windows PowerShell 5.1 decodes a BOM-less .ps1 with the system ANSI code page, so any non-ASCII literal added later would be garbled while arguments passed on the command line stay correct, and the symptom looks like a caller problem. Most editors and file-writing tools strip the BOM, so check again after every edit.
- The script is shared with other repos. Before changing its parameters, defaults or calling form, search every caller as the README section on shared files describes, including the permission settings files.
