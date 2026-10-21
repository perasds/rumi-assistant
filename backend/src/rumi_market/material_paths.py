"""Locations of user-supplied materials; no material is loaded on import."""
import os
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[3]
DEFAULT_APK = PROJECT / 'external/base.apk'


def resolve_apk_path(apk_path=None, *, project=PROJECT) -> Path:
    selected = apk_path if apk_path is not None else os.environ.get('RUMI_APK_PATH') or Path(project) / 'external/base.apk'
    return Path(selected).expanduser()
