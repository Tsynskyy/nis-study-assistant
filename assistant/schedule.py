from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS = {"NisAssistantDigest": ("digest", "-Daily -At 09:00"),
         "NisAssistantCalendar": ("sync-calendar",
                                  "-Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 30)")}


def load_env(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def windowless_python() -> str:
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")
    return str(quiet if quiet.exists() else exe)


def powershell(action: str, python: str, root: Path = ROOT) -> str:
    if action == "remove":
        return "; ".join(f"Unregister-ScheduledTask -TaskName {name} -Confirm:$false -ErrorAction SilentlyContinue"
                         for name in TASKS)
    lines = ["$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries "
             "-AllowStartIfOnBatteries"]
    for name, (cmd, trigger) in TASKS.items():
        lines += [f"$a = New-ScheduledTaskAction -Execute '{python}' -Argument '-m assistant {cmd}' "
                  f"-WorkingDirectory '{root}'",
                  f"$t = New-ScheduledTaskTrigger {trigger}",
                  f"Register-ScheduledTask -TaskName {name} -Action $a -Trigger $t -Settings $s -Force | Out-Null"]
    return "; ".join(lines)


def cron(python: str, root: Path = ROOT) -> str:
    log = root / "logs" / "schedule.log"
    return (f"0 9 * * * cd '{root}' && '{python}' -m assistant digest >> '{log}' 2>&1\n"
            f"*/30 * * * * cd '{root}' && '{python}' -m assistant sync-calendar >> '{log}' 2>&1\n")


def run(action: str) -> str:
    if sys.platform != "win32":
        if action == "install":
            return "Добавьте в crontab -e:\n" + cron(sys.executable)
        return "Удалите строки assistant из crontab -e"
    script = powershell(action, windowless_python())
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(out.stderr.strip() or "PowerShell завершился с ошибкой")
    if action == "install":
        return "Задачи NisAssistantDigest (09:00) и NisAssistantCalendar (каждые 30 минут) созданы"
    return "Задачи удалены"
