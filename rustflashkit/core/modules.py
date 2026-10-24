"""模块（Module）与配置档案（Profile）模型：加载、校验、持久化。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from rustflashkit.core.safety import validate_partition_name

PROFILE_SCHEMA = 1
MODULE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# MSM8916 常见分区，供编辑器下拉提示
COMMON_PARTITIONS = [
    "boot", "recovery", "system", "rootfs", "cache", "userdata", "NSHSTATE",
    "aboot", "sbl1", "rpm", "tz", "hyp", "modem", "splash", "misc",
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class FlashModule:
    """一个可配置的刷写模块 = 一个分区 + 一个镜像 + 若干选项。"""

    id: str
    name: str
    partition: str
    image: str = ""
    enabled: bool = True
    required: bool = False
    description: str = ""
    sha256: str = ""
    allow_missing: bool = False     # 镜像缺失时跳过而不是报错
    allow_bootloader: bool = False  # 引导链分区需显式勾选
    timeout: int = 300              # fastboot flash 超时（秒）
    post_delay: float = 0.0         # 刷完后等待秒数

    # ------------------------------------------------------------------
    def image_path(self, base_dir: Path) -> Path:
        p = Path(self.image).expanduser()
        return p if p.is_absolute() else (base_dir / p)

    def image_exists(self, base_dir: Path) -> bool:
        p = self.image_path(base_dir)
        return p.is_file() and p.stat().st_size > 0

    def compute_sha256(self, base_dir: Path) -> str:
        return sha256_of(self.image_path(base_dir))

    def validate(self, base_dir: Path) -> List[str]:
        errors: List[str] = []
        if not MODULE_ID_RE.match(self.id or ""):
            errors.append(f"模块 id 非法: {self.id!r}（小写字母数字开头，可含 _ -）")
        if not self.name.strip():
            errors.append(f"模块 {self.id}: 名称不能为空")
        perr = validate_partition_name(self.partition or "")
        if perr:
            errors.append(f"模块 {self.id}: {perr}")
        if not self.image.strip():
            errors.append(f"模块 {self.id}: 未设置镜像文件")
        elif self.enabled and not self.image_exists(base_dir):
            hint = "（allow_missing，将跳过）" if self.allow_missing else ""
            errors.append(f"模块 {self.id}: 镜像不存在或为空 {self.image}{hint}")
        if self.timeout < 10:
            errors.append(f"模块 {self.id}: 超时至少 10 秒")
        return errors

    @staticmethod
    def from_dict(d: dict) -> "FlashModule":
        known = {f for f in FlashModule.__dataclass_fields__}
        return FlashModule(**{k: v for k, v in d.items() if k in known})


@dataclass
class Profile:
    """一份完整的刷机配置：机型说明 + 模块列表。"""

    name: str = "未命名配置"
    board: str = ""
    description: str = ""
    modules: List[FlashModule] = field(default_factory=list)
    schema: int = PROFILE_SCHEMA

    # ------------------------------------------------------------------
    def enabled_modules(self) -> List[FlashModule]:
        return [m for m in self.modules if m.enabled]

    def validate(self, base_dir: Path) -> List[str]:
        errors: List[str] = []
        seen_ids, seen_parts = set(), set()
        for m in self.modules:
            errors.extend(m.validate(base_dir))
            if m.id in seen_ids:
                errors.append(f"模块 id 重复: {m.id}")
            if m.partition.strip().lower() in seen_parts:
                errors.append(f"分区重复出现: {m.partition}")
            seen_ids.add(m.id)
            seen_parts.add(m.partition.strip().lower())
        enabled = self.enabled_modules()
        if enabled and not any(m.image_exists(base_dir) or m.allow_missing for m in enabled):
            errors.append("所有启用模块的镜像均不存在，无法生成有效计划")
        return errors

    # ------------------------------------------------------------------
    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @staticmethod
    def load(path: Path) -> "Profile":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Profile 文件格式错误：根元素必须是对象")
        schema = int(data.get("schema", PROFILE_SCHEMA))
        if schema > PROFILE_SCHEMA:
            raise ValueError(f"Profile schema {schema} 高于本工具支持的 {PROFILE_SCHEMA}，请升级工具")
        modules = [FlashModule.from_dict(m) for m in data.get("modules", [])]
        return Profile(
            name=str(data.get("name", "未命名配置")),
            board=str(data.get("board", "")),
            description=str(data.get("description", "")),
            modules=modules,
            schema=schema,
        )


def new_profile_from_template() -> Profile:
    """新建空白配置。"""
    return Profile(name="新建配置", board="", description="")
