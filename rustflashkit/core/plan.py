"""刷机计划（FlashPlan）：把启用的模块编译成可预览、可确认的步骤序列。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from rustflashkit.core.modules import FlashModule, Profile
from rustflashkit.core.safety import PartitionBlocked, check_flashable

DANGER_LABEL = {"bootloader": "引导链·变砖风险", "efs": "EFS·禁止"}


@dataclass
class PlanStep:
    kind: str                 # verify_sha256 | flash | delay | reboot
    title: str
    partition: str = ""
    image: Optional[Path] = None
    timeout: int = 300
    danger: Optional[str] = None   # None | 'bootloader' | 'efs'
    module: Optional[FlashModule] = None
    post_delay: float = 0.0

    def command_repr(self) -> str:
        if self.kind == "flash":
            return f"fastboot flash {self.partition} \"{self.image}\""
        if self.kind == "verify_sha256":
            return f"sha256sum \"{self.image}\""
        if self.kind == "reboot":
            return "fastboot reboot"
        if self.kind == "delay":
            return f"sleep {self.post_delay:g}s"
        return self.title


@dataclass
class PlanOptions:
    reboot_after: bool = True
    verify_sha256: bool = True


@dataclass
class FlashPlan:
    steps: List[PlanStep] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def flashed_partitions(self) -> List[str]:
        return [s.partition for s in self.steps if s.kind == "flash"]

    def danger_partitions(self) -> List[str]:
        return [s.partition for s in self.steps
                if s.kind == "flash" and s.danger == "bootloader"]

    def summary_lines(self) -> List[str]:
        lines = []
        parts = self.flashed_partitions()
        lines.append(f"共 {len(parts)} 个写操作: {', '.join(parts) if parts else '（无）'}")
        danger = self.danger_partitions()
        if danger:
            lines.append(f"⚠ 引导链分区（变砖风险）: {', '.join(danger)}")
        if self.skipped:
            lines.append("跳过的模块: " + "; ".join(self.skipped))
        if self.errors:
            lines.append("‼ 存在问题，不能执行:")
            lines.extend(f"  - {e}" for e in self.errors)
        return lines


def build_plan(profile: Profile, base_dir: Path, options: PlanOptions) -> FlashPlan:
    """由 Profile + 选项生成计划。所有校验问题进入 plan.errors，绝不静默放过。"""
    plan = FlashPlan()

    for mod in profile.enabled_modules():
        if not mod.image_exists(base_dir):
            if mod.allow_missing:
                plan.skipped.append(f"{mod.name}({mod.partition})：镜像缺失，跳过")
                continue
            plan.errors.append(f"{mod.name}: 镜像不存在 {mod.image}")
            continue

        try:
            check_flashable(mod.partition, allow_bootloader=mod.allow_bootloader)
        except PartitionBlocked as exc:
            plan.errors.append(f"{mod.name}({mod.partition}): {exc}")
            continue

        danger = None
        from rustflashkit.core.safety import assess
        lvl = assess(mod.partition)
        if lvl:
            danger = lvl

        image = mod.image_path(base_dir)
        if options.verify_sha256 and mod.sha256:
            plan.steps.append(PlanStep(
                kind="verify_sha256", title=f"校验 {mod.name} SHA256",
                image=image, module=mod))
        plan.steps.append(PlanStep(
            kind="flash", title=f"刷写 {mod.name} → {mod.partition}",
            partition=mod.partition.strip(), image=image,
            timeout=max(30, int(mod.timeout)), danger=danger, module=mod,
            post_delay=mod.post_delay))

    if options.reboot_after and plan.steps:
        plan.steps.append(PlanStep(kind="reboot", title="刷写完成，重启设备", timeout=60))

    if not plan.steps and plan.ok:
        plan.errors.append("没有可执行的步骤（未启用任何模块或镜像均缺失）")
    return plan
