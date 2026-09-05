from __future__ import annotations

import os
import shutil
import subprocess
import sys


def send_notification(title: str, message: str) -> bool:
    """Send a best-effort desktop notification without failing the caller."""
    try:
        if sys.platform == "win32":
            return _notify_windows(title, message)
        if sys.platform == "darwin":
            return _notify_macos(title, message)
        return _notify_linux(title, message)
    except (OSError, subprocess.SubprocessError):
        return False


def _notify_windows(title: str, message: str) -> bool:
    executable = shutil.which("powershell.exe") or shutil.which("powershell")
    if executable is None:
        return False
    env = os.environ.copy()
    env["PYNEOLINK_NOTIFICATION_TITLE"] = title
    env["PYNEOLINK_NOTIFICATION_MESSAGE"] = message
    script = (
        "$title=[Environment]::GetEnvironmentVariable('PYNEOLINK_NOTIFICATION_TITLE');"
        "$message=[Environment]::GetEnvironmentVariable('PYNEOLINK_NOTIFICATION_MESSAGE');"
        "[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]"
        "> $null;"
        "$template=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
        "$nodes=$template.GetElementsByTagName('text');"
        "$nodes.Item(0).AppendChild($template.CreateTextNode($title)) > $null;"
        "$nodes.Item(1).AppendChild($template.CreateTextNode($message)) > $null;"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($template);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('PyNeolink').Show($toast)"
    )
    result = subprocess.run(
        [executable, "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        check=False,
        env=env,
        timeout=5,
    )
    return result.returncode == 0


def _notify_macos(title: str, message: str) -> bool:
    executable = shutil.which("osascript")
    if executable is None:
        return False
    script = "display notification (item 2 of argv) with title (item 1 of argv)"
    result = subprocess.run(
        [executable, "-e", script, title, message],
        capture_output=True,
        check=False,
        timeout=5,
    )
    return result.returncode == 0


def _notify_linux(title: str, message: str) -> bool:
    executable = shutil.which("notify-send")
    if executable is None:
        return False
    result = subprocess.run(
        [executable, "--app-name=PyNeolink", title, message],
        capture_output=True,
        check=False,
        timeout=5,
    )
    return result.returncode == 0
