"""应用设置（config/settings.json）与会话日志（logs/）。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from rustflashkit.core.runner import app_root


@dataclass
class Options:
    reboot_after: bool = True
    verify_sha256: bool = True


@dataclass
class Settings:
    adb_path: str = ""
    fastboot_path: str = ""
    last_profile: str = ""
    options: Options = field(default_factory=Options)

    # ------------------------------------------------------------------
    _PATH: Optional[Path] = None  # 类级缓存（由 load 设置）

    @classmethod
    def path(cls) -> Path:
        if cls._PATH is None:
            cls._PATH = app_root() / "config" / "settings.json"
        return cls._PATH

    def save(self) -> None:
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2),
                     encoding="utf-8")

    @classmethod
    def load(cls) -> "Settings":
        p = cls.path()
        if not p.is_file():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            opts = data.get("options", {}) or {}
            s = cls(
                adb_path=str(data.get("adb_path", "")),
                fastboot_path=str(data.get("fastboot_path", "")),
                last_profile=str(data.get("last_profile", "")),
                options=Options(**{k: opts[k] for k in Options.__dataclass_fields__ if k in opts}),
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return cls()
        return s


class SessionLog:
    """每次刷机会话写入 logs/flash-<时间戳>.log。"""

    def __init__(self):
        logs_dir = app_root() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = logs_dir / f"flash-{stamp}.log"
        self._fh = None

    def start(self) -> None:
        self._fh = open(self.path, "a", encoding="utf-8")

    def write(self, line: str) -> None:
        if self._fh is None:
            return
        stamp = datetime.now().strftime("%H:%M:%S")
        self._fh.write(f"[{stamp}] {line}\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
