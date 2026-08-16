"""Persistente Konfiguration in /config/config.json.

Alles, was der Nutzer sonst als Umgebungsvariable setzen muesste, wird hier im
Browser eingerichtet und im Volume abgelegt. So braucht die TrueNAS-Installation
nur Image + Port + ein Storage-Volume.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_FILE = CONFIG_DIR / "config.json"

_DEFAULTS: dict[str, Any] = {
    "setup_done": False,
    "admin_hash": "",
    "admin_salt": "",
    "tuya": {"client_id": "", "client_secret": "", "region": "eu"},
    "device_id": "",
    "device_name": "",
    "api_token": "",
    "refresh_seconds": 10,
    "history_seconds": 60,
    "tuya_setup_ts": 0,        # wann die Tuya-Zugangsdaten zuletzt bestaetigt wurden
    "trial_reminder_days": 25,  # ab wann an die Verlaengerung erinnert wird
    "tibber": {"token": "", "home_id": "", "home_label": ""},
    "price": {},
    "automation": {},
    "override_until": 0,
}


def _atomic_write(path: Path, payload: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


class Config:
    def __init__(self) -> None:
        self._data: dict[str, Any] = json.loads(json.dumps(_DEFAULTS))
        self.load()

    # ------------------------------------------------------------------ I/O

    def load(self) -> None:
        if not CONFIG_FILE.exists():
            return
        try:
            stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for key, value in stored.items():
            if key in self._data and isinstance(self._data[key], dict) and isinstance(value, dict):
                self._data[key].update(value)
            else:
                self._data[key] = value

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write(CONFIG_FILE, json.dumps(self._data, indent=2, ensure_ascii=False))
        try:
            CONFIG_FILE.chmod(0o600)
        except OSError:
            pass  # z.B. auf manchen Netz-Shares nicht erlaubt

    # ------------------------------------------------------------------ Zugriff

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    @property
    def setup_done(self) -> bool:
        return bool(self._data.get("setup_done"))

    @property
    def tuya(self) -> dict[str, str]:
        return dict(self._data.get("tuya", {}))

    def set_tuya(self, client_id: str, client_secret: str, region: str) -> None:
        self._data["tuya"] = {
            "client_id": client_id.strip(),
            "client_secret": client_secret.strip(),
            "region": region.strip() or "eu",
        }

    def has_tuya_credentials(self) -> bool:
        t = self._data.get("tuya", {})
        return bool(t.get("client_id") and t.get("client_secret"))

    # ------------------------------------------------------------------ Passwort

    def set_admin_password(self, password: str) -> None:
        salt = secrets.token_hex(16)
        self._data["admin_salt"] = salt
        self._data["admin_hash"] = self._hash(password, salt)

    def check_admin_password(self, password: str) -> bool:
        stored = self._data.get("admin_hash", "")
        salt = self._data.get("admin_salt", "")
        if not stored or not salt:
            return False
        return secrets.compare_digest(stored, self._hash(password, salt))

    @staticmethod
    def _hash(password: str, salt: str) -> str:
        return hashlib.scrypt(
            password.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=32
        ).hex()

    # ------------------------------------------------------------------ API-Token

    def ensure_api_token(self) -> str:
        if not self._data.get("api_token"):
            self._data["api_token"] = secrets.token_urlsafe(32)
            self.save()
        return self._data["api_token"]

    def rotate_api_token(self) -> str:
        self._data["api_token"] = secrets.token_urlsafe(32)
        self.save()
        return self._data["api_token"]

    # ------------------------------------------------------------------ Session

    def session_secret(self) -> str:
        """Cookie-Signaturschluessel; ueberlebt Neustarts, damit Logins halten."""
        if not self._data.get("session_secret"):
            self._data["session_secret"] = secrets.token_urlsafe(48)
            self.save()
        return self._data["session_secret"]


config = Config()
