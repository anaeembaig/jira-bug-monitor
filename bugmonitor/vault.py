"""Keep tokens and webhook URLs out of config.toml.

Order of preference: the OS credential store via `keyring` (macOS Keychain, Windows
Credential Locker, Secret Service on Linux), then the macOS `security` command, then
nothing (the caller falls back to config.toml or environment variables).
"""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess

log = logging.getLogger("bugmonitor.secrets")
SERVICE = "bugmonitor"
NAMES = {
    "jira_api_token": "Jira API token",
    "github_token": "GitHub token",
    "jenkins_token": "Jenkins API token",
    "teams_webhook": "Teams webhook (your private chat)",
    "slack_webhook": "Slack webhook",
    "smtp_password": "SMTP password",
}


def _keyring():
    try:
        import keyring  # optional dependency
        from keyring.errors import NoKeyringError  # noqa: F401
        backend = keyring.get_keyring()
        if "fail" in type(backend).__module__.lower():
            return None
        return keyring
    except Exception:
        return None


def _mac_security() -> str | None:
    return shutil.which("security") if platform.system() == "Darwin" else None


def get_secret(name: str) -> str:
    kr = _keyring()
    if kr:
        try:
            return kr.get_password(SERVICE, name) or ""
        except Exception as e:
            log.debug("keyring read failed: %s", e)
    sec = _mac_security()
    if sec:
        r = subprocess.run([sec, "find-generic-password", "-s", SERVICE, "-a", name, "-w"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    return ""


def set_secret(name: str, value: str) -> bool:
    kr = _keyring()
    if kr:
        try:
            kr.set_password(SERVICE, name, value)
            return True
        except Exception as e:
            log.debug("keyring write failed: %s", e)
    sec = _mac_security()
    if sec:
        r = subprocess.run([sec, "add-generic-password", "-U", "-s", SERVICE, "-a", name, "-w", value],
                           capture_output=True, text=True)
        return r.returncode == 0
    return False


def delete_secret(name: str) -> None:
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(SERVICE, name)
            return
        except Exception:
            pass
    sec = _mac_security()
    if sec:
        subprocess.run([sec, "delete-generic-password", "-s", SERVICE, "-a", name], capture_output=True)


def store_available() -> bool:
    return bool(_keyring() or _mac_security())
