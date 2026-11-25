"""Daily user-level scheduling for the Sandbox client."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile

from sandbox.controller_update import engine, gitops, hook
from sandbox.controller_update.models import NoAction, Recovered, Skip, SkipReason, Updated


def _enabled() -> bool:
    if os.environ.get(hook.ENV_JOB_CHILD) or os.environ.get(hook.ENV_REEXEC_GUARD):
        return False
    if os.environ.get("SB_RELEASE_REVISION"):
        return False
    setting = hook.env_policy(os.environ)
    return hook.config_enabled() if setting is None else setting


def _name(root: Path) -> str:
    suffix = hashlib.sha256(os.fsencode(root.resolve())).hexdigest()[:12]
    return f"sandbox-client-update-{suffix}"


def _run(*argv: str) -> bool:
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=15, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _write(path: Path, contents: bytes) -> bool:
    if not path.is_symlink() and path.is_file() and path.read_bytes() == contents:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as folder:
        staged = Path(folder) / path.name
        staged.write_bytes(contents)
        os.chmod(staged, 0o600)
        os.replace(staged, path)
    return True


def _systemd_arg(value: str) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("the checkout path contains a control character")
    return ('"' + value.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("%", "%%") + '"')


def _systemd_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd/user"


def _mac_unit(root: Path) -> tuple[str, Path]:
    label = "dev.sandbox." + _name(root)
    return label, Path.home() / "Library/LaunchAgents" / f"{label}.plist"


def install_schedule(root: Path) -> tuple[bool, str] | None:
    """Return the scheduler result, or None for ineligible checkouts."""
    root = root.resolve()
    if not _enabled() or not engine.tracks_main(gitops.read_checkout(root, timeout=5)):
        return None
    name = _name(root)
    argv = ["/bin/sh", str(root / "sb"), "client-update", "now"]
    try:
        if sys.platform == "darwin":
            label, plist = _mac_unit(root)
            domain = f"gui/{os.getuid()}"
            log = engine.default_lock_dir().parent / f"{name}.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            changed = _write(plist, plistlib.dumps({
                "Label": label, "ProgramArguments": argv,
                "WorkingDirectory": str(root),
                "StartCalendarInterval": {"Hour": 9, "Minute": 0},
                "ProcessType": "Background",
                "StandardOutPath": str(log),
                "StandardErrorPath": str(log),
                "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
            }))
            ident = f"{domain}/{label}"
            if changed:
                _run("launchctl", "bootout", ident)
                enabled = _run("launchctl", "bootstrap", domain, str(plist))
            else:
                enabled = (_run("launchctl", "print", ident)
                           or _run("launchctl", "bootstrap", domain, str(plist)))
        elif sys.platform.startswith("linux") and shutil.which("systemctl"):
            folder = _systemd_dir()
            command = " ".join(_systemd_arg(arg.replace("$", "$$")) for arg in argv)
            _write(folder / f"{name}.service", (
                "[Service]\nType=oneshot\nTimeoutStartSec=7min\n"
                f"ExecStart={command}\n"
            ).encode())
            _write(folder / f"{name}.timer", (
                "[Timer]\nOnCalendar=*-*-* 09:00:00\nPersistent=true\n"
                "[Install]\nWantedBy=timers.target\n"
            ).encode())
            enabled = (_run("systemctl", "--user", "daemon-reload")
                       and _run("systemctl", "--user", "enable", "--now", f"{name}.timer"))
        else:
            return False, "Daily client updates need launchd or a systemd user session."
    except (OSError, ValueError) as exc:
        return False, f"Could not install daily client updates ({exc})."
    if enabled:
        return True, "Sandbox checks origin/main for client updates daily at 09:00 local time."
    return False, "Could not enable daily client updates. Run ./sb client-update schedule in a user session."


def remove_schedule(root: Path) -> None:
    if sys.platform == "darwin":
        label, path = _mac_unit(root)
        if path.exists():
            _run("launchctl", "bootout", f"gui/{os.getuid()}/{label}")
            path.unlink()
    elif sys.platform.startswith("linux"):
        name = _name(root)
        folder = _systemd_dir()
        timer = folder / f"{name}.timer"
        service = folder / f"{name}.service"
        if timer.exists() or service.exists():
            _run("systemctl", "--user", "disable", "--now", timer.name)
            timer.unlink(missing_ok=True)
            service.unlink(missing_ok=True)
            _run("systemctl", "--user", "daemon-reload")


def run(action: str, root: Path) -> int:
    if action == "schedule":
        result = install_schedule(root)
        if result is None:
            print("This checkout is pinned, disabled, or does not track origin/main.")
            return 0
        enabled, message = result
        print(message, file=sys.stdout if enabled else sys.stderr)
        return int(not enabled)
    if action == "disable":
        remove_schedule(root)
        print("Removed this checkout's daily client update schedule.")
        return 0
    if not _enabled():
        print("Client updates are disabled for this checkout.")
        return 0
    result = engine.follow_main(root)
    if isinstance(result, Recovered):
        print(result.notice, file=sys.stderr)
        result = engine.follow_main(root)
    if isinstance(result, Updated):
        for notice in result.notices:
            print(notice)
        print(f"Updated Sandbox from {result.old[:7]} to {result.new[:7]}.")
    elif isinstance(result, NoAction):
        if result.reason is SkipReason.UP_TO_DATE:
            print("Sandbox is up to date.")
        else:
            print("This checkout does not track origin/main.")
    elif isinstance(result, Skip):
        print(result.notice, file=sys.stderr)
        return int(result.reason in (SkipReason.FETCH_FAILED, SkipReason.FF_FAILED))
    else:
        print(result.notice, file=sys.stderr)
        return int(not isinstance(result, Recovered))
    return 0
