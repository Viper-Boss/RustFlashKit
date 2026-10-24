"""对话框：模块编辑器、刷机计划预览与确认、工具路径设置、关于。"""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional

from rustflashkit import APP_NAME, __version__
from rustflashkit.core.config import Settings
from rustflashkit.core.modules import COMMON_PARTITIONS, FlashModule
from rustflashkit.core.plan import FlashPlan

CONFIRM_WORD_BL = "确认引导链"
FIELD_W = 46  # 表单控件统一宽度


class ModalDialog(tk.Toplevel):
    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self.result: Optional[FlashModule] = None
        self.frame = ttk.Frame(self, padding=12)
        self.frame.pack(fill="both", expand=True)


# ======================================================================
# 模块编辑器
# ======================================================================
class ModuleEditor(ModalDialog):
    def __init__(self, parent, module: Optional[FlashModule] = None,
                 base_dir: Path = None, used_ids: set = None):
        super().__init__(parent, "模块编辑")
        self.base_dir = base_dir or Path.cwd()
        self.used_ids = used_ids or set()
        self.editing = module is not None
        m = module or FlashModule(id="", name="", partition="")
        self._m = m

        frm = self.frame
        self.v_id = tk.StringVar(value=m.id)
        self.v_name = tk.StringVar(value=m.name)
        self.v_part = tk.StringVar(value=m.partition)
        self.v_img = tk.StringVar(value=m.image)
        self.v_desc = tk.StringVar(value=m.description)
        self.v_sha = tk.StringVar(value=m.sha256)

        def row(r: int, label: str, widget):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=r, column=1, columnspan=2, sticky="we", pady=3)
            return widget

        row(0, "模块 ID*", ttk.Entry(frm, textvariable=self.v_id, width=FIELD_W, state=(
            "disabled" if self.editing else "normal")))
        row(1, "模块名称*", ttk.Entry(frm, textvariable=self.v_name, width=FIELD_W))
        row(2, "分区名*", ttk.Combobox(frm, textvariable=self.v_part, width=FIELD_W - 2,
                                       values=COMMON_PARTITIONS))

        img_frm = ttk.Frame(frm)
        ttk.Entry(img_frm, textvariable=self.v_img).pack(side="left", fill="x", expand=True)
        ttk.Button(img_frm, text="浏览…", command=self._browse_image, width=8).pack(side="left", padx=(6, 0))
        row(3, "镜像文件*", img_frm)
        row(4, "说明", ttk.Entry(frm, textvariable=self.v_desc, width=FIELD_W))

        sha_frm = ttk.Frame(frm)
        ttk.Entry(sha_frm, textvariable=self.v_sha).pack(side="left", fill="x", expand=True)
        ttk.Button(sha_frm, text="计算…", command=self._compute_sha, width=8).pack(side="left", padx=(6, 0))
        row(5, "SHA256 (可选)", sha_frm)

        opt = ttk.LabelFrame(frm, text="选项", padding=8)
        opt.grid(row=6, column=0, columnspan=3, sticky="we", pady=(8, 4))
        self.v_enabled = tk.BooleanVar(value=m.enabled)
        self.v_required = tk.BooleanVar(value=m.required)
        self.v_allow_missing = tk.BooleanVar(value=m.allow_missing)
        self.v_allow_bl = tk.BooleanVar(value=m.allow_bootloader)
        ttk.Checkbutton(opt, text="启用（参与刷写）", variable=self.v_enabled).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opt, text="必需（不可停用）", variable=self.v_required).grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(opt, text="镜像缺失时跳过", variable=self.v_allow_missing).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(opt, text="允许引导链分区（高危）", variable=self.v_allow_bl).grid(row=1, column=1, sticky="w")
        ttk.Label(opt, text="flash 超时(秒)").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.v_timeout = tk.IntVar(value=m.timeout)
        ttk.Spinbox(opt, from_=30, to=3600, increment=30, textvariable=self.v_timeout, width=8).grid(
            row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Label(opt, text="刷后等待(秒)").grid(row=3, column=0, sticky="w")
        self.v_delay = tk.DoubleVar(value=m.post_delay)
        ttk.Spinbox(opt, from_=0, to=30, increment=0.5, textvariable=self.v_delay, width=8).grid(
            row=3, column=1, sticky="w")

        btns = ttk.Frame(frm)
        btns.grid(row=7, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="确定", command=self._on_ok).pack(side="right", padx=4)
        ttk.Label(frm, text="* 必填；镜像路径可写相对路径（相对配置文件目录）",
                  foreground="#666").grid(row=8, column=0, columnspan=3, sticky="w", pady=(6, 0))
        frm.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------
    def _browse_image(self) -> None:
        fn = filedialog.askopenfilename(title="选择镜像文件", initialdir=str(self.base_dir))
        if fn:
            p = Path(fn)
            try:
                rel = p.resolve().relative_to(self.base_dir.resolve())
                self.v_img.set(str(rel))
            except ValueError:
                self.v_img.set(str(p))

    def _compute_sha(self) -> None:
        p = self._image_path()
        if p is None:
            messagebox.showwarning("提示", "请先填写存在的镜像文件路径。", parent=self)
            return
        try:
            from rustflashkit.core.modules import sha256_of
            self.v_sha.set(sha256_of(p))
        except OSError as exc:
            messagebox.showerror("失败", str(exc), parent=self)

    def _image_path(self) -> Optional[Path]:
        raw = self.v_img.get().strip()
        if not raw:
            return None
        p = Path(raw).expanduser()
        p = p if p.is_absolute() else self.base_dir / p
        return p if p.is_file() else None

    def _on_ok(self) -> None:
        new = FlashModule(
            id=self._m.id if self.editing else self.v_id.get().strip().lower(),
            name=self.v_name.get().strip(),
            partition=self.v_part.get().strip(),
            image=self.v_img.get().strip(),
            enabled=self.v_enabled.get(),
            required=self.v_required.get(),
            description=self.v_desc.get().strip(),
            sha256=self.v_sha.get().strip().lower(),
            allow_missing=self.v_allow_missing.get(),
            allow_bootloader=self.v_allow_bl.get(),
            timeout=self.v_timeout.get(),
            post_delay=self.v_delay.get(),
        )
        if not self.editing and new.id in self.used_ids:
            messagebox.showwarning("重复", f"模块 ID「{new.id}」已存在。", parent=self)
            return
        errors = new.validate(self.base_dir)
        errors = [e for e in errors if "镜像不存在" not in e or not new.allow_missing] or errors
        if errors and not messagebox.askyesno(
                "校验提示", "模块存在以下问题：\n\n" + "\n".join(f"· {e}" for e in errors)
                + "\n\n仍要保存吗？（镜像缺失也可稍后补齐）", parent=self):
            return
        self.result = new
        self.destroy()


# ======================================================================
# 刷机计划预览 / 确认
# ======================================================================
class PlanPreviewDialog(tk.Toplevel):
    def __init__(self, parent, plan: FlashPlan):
        super().__init__(parent)
        self.title("刷机计划预览与确认")
        self.transient(parent)
        self.grab_set()
        self.plan = plan
        self.confirmed = False
        self.geometry("720x560")

        head = ttk.Frame(self, padding=(12, 10))
        head.pack(fill="x")
        ttk.Label(head, text="执行以下操作前请逐条确认：", font=("", 11, "bold")).pack(anchor="w")
        summary = tk.Text(head, height=4, wrap="word", bd=0, bg=self.cget("bg"))
        summary.pack(fill="x", pady=(4, 0))
        for line in plan.summary_lines():
            summary.insert("end", line + "\n", "warn" if ("⚠" in line or "‼" in line) else "norm")
        summary.tag_configure("warn", foreground="#b06000")
        summary.tag_configure("norm", foreground="#333")
        summary.configure(state="disabled")

        import tkinter.scrolledtext as st
        body = st.ScrolledText(self, font=("Consolas", 10))
        body.pack(fill="both", expand=True, padx=12)
        for i, step in enumerate(plan.steps, 1):
            danger_mark = "  ⚠" if step.danger == "bootloader" else ""
            body.insert("end", f"{i:2d}. {step.title}{danger_mark}\n",
                        "danger" if step.danger else "step")
            body.insert("end", f"    $ {step.command_repr()}\n", "cmd")
        body.tag_configure("step", foreground="#111")
        body.tag_configure("cmd", foreground="#045")
        body.tag_configure("danger", foreground="#b06000", font=("Consolas", 10, "bold"))
        body.configure(state="disabled")

        foot = ttk.Frame(self, padding=(12, 10))
        foot.pack(fill="x")
        self.v_ack = tk.BooleanVar(value=False)
        self.ack = ttk.Checkbutton(
            foot, text="我已逐条核对以上命令，确认镜像来源可信、分区无误，理解刷机可能变砖",
            variable=self.v_ack, command=self._update_state)
        self.ack.pack(anchor="w")

        self.danger_row = ttk.Frame(foot)
        self.danger_row.pack(anchor="w", pady=(6, 0))
        ttk.Label(self.danger_row,
                  text=f"⚠ 含引导链分区，需输入「{CONFIRM_WORD_BL}」：").pack(side="left")
        self.v_confirm_word = tk.StringVar()
        self.word_entry = ttk.Entry(self.danger_row, textvariable=self.v_confirm_word, width=12)
        self.word_entry.pack(side="left", padx=4)
        self.word_entry.bind("<KeyRelease>", lambda e: self._update_state())
        has_danger = bool(plan.danger_partitions())
        if not has_danger:
            self.danger_row.pack_forget()

        if not plan.ok:
            ttk.Label(foot, text="‼ 计划存在错误，不能执行（见上方汇总）",
                      foreground="#c00").pack(anchor="w", pady=(4, 0))

        btns = ttk.Frame(foot)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=4)
        self.btn_go = ttk.Button(btns, text="确认执行", style="Flash.TButton",
                                 command=self._on_go, state="disabled")
        self.btn_go.pack(side="right", padx=4)
        self._update_state()

    def _update_state(self) -> None:
        ok = self.plan.ok and self.v_ack.get()
        if self.plan.danger_partitions():
            ok = ok and self.v_confirm_word.get().strip() == CONFIRM_WORD_BL
        self.btn_go.configure(state="normal" if ok else "disabled")

    def _on_go(self) -> None:
        self.confirmed = True
        self.destroy()


# ======================================================================
# 设置对话框
# ======================================================================
class SettingsDialog(ModalDialog):
    def __init__(self, parent, settings: Settings, on_saved: Callable[[], None] = lambda: None):
        super().__init__(parent, "设置")
        self.settings = settings
        self.on_saved = on_saved
        frm = self.frame
        self.v_adb = tk.StringVar(value=settings.adb_path)
        self.v_fb = tk.StringVar(value=settings.fastboot_path)

        ttk.Label(frm, text="提示：留空则依次查找 应用目录/tools/、系统 PATH。", foreground="#666").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        def tool_row(r: int, label: str, var: tk.StringVar):
            ttk.Label(frm, text=label).grid(row=r, column=0, sticky="w", pady=4, padx=(0, 8))
            ttk.Entry(frm, textvariable=var).grid(row=r, column=1, sticky="we", pady=4)
            ttk.Button(frm, text="浏览…", width=8,
                       command=lambda: self._pick(var)).grid(row=r, column=2, padx=4)

        tool_row(1, "adb 路径", self.v_adb)
        tool_row(2, "fastboot 路径", self.v_fb)
        ttk.Label(frm, foreground="#666",
                  text="下载 platform-tools: https://developer.android.com/tools/releases/platform-tools").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="保存", command=self._save).pack(side="right", padx=4)
        frm.columnconfigure(1, weight=1)

    def _pick(self, var: tk.StringVar) -> None:
        fn = filedialog.askopenfilename(title="选择可执行文件",
                                        filetypes=[("可执行文件", "*.*")])
        if fn:
            var.set(fn)

    def _save(self) -> None:
        self.settings.adb_path = self.v_adb.get().strip()
        self.settings.fastboot_path = self.v_fb.get().strip()
        self.settings.save()
        self.on_saved()
        self.destroy()


# ======================================================================
# 小工具
# ======================================================================
def ask_profile_name(parent) -> str:
    dlg = tk.Toplevel(parent)
    dlg.title("新建配置")
    dlg.transient(parent)
    dlg.grab_set()
    dlg.resizable(False, False)
    frm = ttk.Frame(dlg, padding=14)
    frm.pack()
    ttk.Label(frm, text="配置名称:").grid(row=0, column=0, pady=6)
    var = tk.StringVar(value="我的 MSM8916 配置")
    ttk.Entry(frm, textvariable=var, width=28).grid(row=0, column=1, pady=6, padx=6)
    ttk.Button(frm, text="创建", command=dlg.destroy).grid(row=1, column=0, columnspan=2, pady=(8, 0))
    dlg.bind("<Return>", lambda e: dlg.destroy())
    dlg.wait_visibility()
    dlg.wait_window()
    return var.get().strip()


def show_about() -> None:
    messagebox.showinfo(
        "关于",
        f"{APP_NAME} v{__version__}\n\n"
        "MSM8916（骁龙410 / UFI 棒子）刷机与模块配置工具。\n"
        "独立项目：与 RustSimHub 仓库互不修改，镜像文件由使用者自行提供。\n\n"
        "刷机有风险，操作前请阅读 README 安全须知。")
