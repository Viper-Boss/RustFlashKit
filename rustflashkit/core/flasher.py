"""计划执行器：工作线程内逐步执行，失败即停，可取消。"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from rustflashkit.core.device import DeviceService
from rustflashkit.core.modules import sha256_of
from rustflashkit.core.plan import FlashPlan, PlanStep
from rustflashkit.core.runner import ToolRunner


@dataclass
class StepResult:
    index: int
    step: PlanStep
    ok: bool
    detail: str = ""


class FlashExecutor:
    """在后台线程顺序执行 FlashPlan。

    回调（on_log/on_step/on_done）在工作线程中调用，
    GUI 侧需自行通过 queue/after 转回主线程。
    """

    def __init__(self, plan: FlashPlan, runner: ToolRunner, device: DeviceService,
                 on_log: Callable[[str], None],
                 on_step: Callable[[StepResult], None],
                 on_done: Callable[[bool, str], None],
                 serial: str = ""):
        self.plan = plan
        self.runner = runner
        self.device = device
        self.serial = serial  # 目标设备序列号（多设备时显式指定 fastboot -s）
        self.on_log = on_log
        self.on_step = on_step
        self.on_done = on_done
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _fb_args(self, args: list[str]) -> list[str]:
        return (["-s", self.serial] if self.serial else []) + args

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="flasher", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        self.on_log("⏹ 用户请求取消（将在当前步骤结束后停止）…")

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ------------------------------------------------------------------
    def _run(self) -> None:
        self.on_log(f"▶ 开始执行刷机计划：共 {len(self.plan.steps)} 步")
        ok_all = True
        message = "全部完成"
        for i, step in enumerate(self.plan.steps):
            if self._cancel.is_set():
                ok_all = False
                message = "已取消"
                self.on_log(f"⏹ 第 {i + 1} 步前取消")
                break
            ok, detail = self._exec_step(step)
            self.on_step(StepResult(i, step, ok, detail))
            if not ok:
                ok_all = False
                message = f"第 {i + 1} 步失败：{step.title} — {detail}"
                self.on_log(f"✗ {message}，已停止后续步骤")
                break
        self.on_done(ok_all, message)

    def _exec_step(self, step: PlanStep) -> tuple[bool, str]:
        if step.kind == "verify_sha256":
            mod = step.module
            try:
                actual = sha256_of(step.image)
            except OSError as exc:
                return False, f"读取镜像失败: {exc}"
            if actual.lower() != mod.sha256.lower():
                return False, f"SHA256 不匹配（期望 {mod.sha256[:12]}…，实际 {actual[:12]}…）"
            self.on_log(f"✓ SHA256 校验通过: {step.image.name}")
            return True, ""

        if step.kind == "flash":
            res = self.runner.run("fastboot",
                                  self._fb_args(["flash", step.partition, str(step.image)]),
                                  timeout=step.timeout)
            if not res.ok:
                return False, (res.stderr or res.stdout or "fastboot 返回非零").strip()[:400]
            self.on_log(f"✓ 已刷写 {step.partition}")
            if step.post_delay > 0:
                time.sleep(min(step.post_delay, 30))
            return True, ""

        if step.kind == "reboot":
            res = self.runner.run("fastboot", self._fb_args(["reboot"]), timeout=30)
            return (True, "") if res.ok else (False, (res.stderr or "reboot 失败").strip()[:200])

        if step.kind == "delay":
            time.sleep(min(step.post_delay, 30))
            return True, ""

        return False, f"未知步骤类型 {step.kind}"
