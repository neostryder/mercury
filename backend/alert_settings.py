"""The owner's choice of where Mercury's notifications go, kept in one small JSON file.

`off` uses Mercury's own delivery, `both` adds Cresset copies, and `cresset` tries Cresset first for flagged-message reports. A saved choice pins the mode and skips the registry. MERCURY_CRESSET_MODE is the fallback when the registry has no answer. The file is replaced atomically so an interruption cannot leave half a setting behind."""
import json
import os
import tempfile
from pathlib import Path

MODES = ("off", "both", "cresset")


def _path() -> Path:
    return Path(os.environ.get("MERCURY_ALERT_SETTINGS_PATH", "/data/alert_settings.json"))


def saved_mode() -> str | None:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = data.get("cresset") if isinstance(data, dict) else None
    return mode if mode in MODES else None


def environment_mode() -> str:
    env = os.environ.get("MERCURY_CRESSET_MODE", "off").strip().lower()
    return env if env in MODES else "off"


def current_mode() -> str:
    return saved_mode() or environment_mode()


def save_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".alert_settings.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as out:
            json.dump({"cresset": mode}, out)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise
