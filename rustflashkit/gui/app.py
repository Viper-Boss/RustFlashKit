"""RustFlashKit 主窗口 — 现代扁平主题。

布局：深色顶栏（标题 + 设备徽章） → 工具栏 → 左侧卡片（设备/选项/动作）
+ 右侧卡片（模块表格 / 控制台式日志） → 状态栏。
"""
from __future__ import annotations

import queue
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from rustflashkit import APP_NAME, __version__
from rustflashkit.core.config import SessionLog, Settings
from rustflashkit.core.device import DeviceInfo, DeviceService
from rustflashkit.core.flasher import FlashExecutor, StepResult
from rustflashkit.core.modules import Profile
from rustflashkit.core.plan import PlanOptions, build_plan
from rustflashkit.core.runner import ToolLocator, ToolRunner, app_root
from rustflashkit.gui.dialogs import (ModuleEditor, PlanPreviewDialog,
                                      SettingsDialog, show_about)
from rustflashkit.gui.theme import PALETTE

POLL_MS = 2000


class MainApp(tk.Tk):
    def __init__(self):
        super().__init__()
        P = PALETTE
        self.title(f"{APP_NAME} — MSM8916 刷机与模块配置工具")
        self.geometry("1120x780")
        self.minsize(980, 660)
        self.configure(bg=P["bg"])

        self._init_fonts()

        self.settings = Settings.load()
        self.locator = ToolLocator(self.settings.adb_path, self.settings.fastboot_path)
        self.runner = ToolRunner(self.locator, log=self.log)
        self.device = DeviceService(self.runner)

        self.profile: Optional[Profile] = None
        self.profile_path: Optional[Path] = None
        self.base_dir: Path = app_root()

        self.executor: Optional[FlashExecutor] = None
        self.session_log: Optional[SessionLog] = None
        self.ui_queue: "queue.Queue[tuple]" = queue.Queue()
        self._export_open = False

        self._build_style()
        self._build_menu()
        self._build_header()
        self._build_toolbar()
        self._build_statusbar()   # 先占用底部，避免被主内容挤出
        self._build_body()

        self.after(80, self._drain_queue)
        self.after(300, self._poll_device)
        self._load_initial_profile()
        self.log(f"{APP_NAME} v{__version__} 就绪。镜像文件由用户自行提供，刷机前请务必阅读 README 安全须知。")

    # ==================================================================
    # 字体与主题
    # ==================================================================
    def _init_fonts(self) -> None:
        is_win = self.tk.call("tk", "windowingsystem") == "win32"
        families = set(tkfont.families(self))
        if is_win:
            ui = "Microsoft YaHei UI"
            mono = "Consolas"
        else:
            ui = next((f for f in ("Noto Sans CJK SC", "Noto Sans CJK JP",
                                   "WenQuanYi Micro Hei") if f in families),
                      "TkDefaultFont")
            mono = next((f for f in ("Noto Sans Mono CJK SC", "DejaVu Sans Mono")
                         if f in families), "TkFixedFont")
        self.f_ui = tkfont.Font(family=ui, size=10)
        self.f_ui_bold = tkfont.Font(family=ui, size=10, weight="bold")
        self.f_title = tkfont.Font(family=ui, size=14, weight="bold")
        self.f_chip = tkfont.Font(family=ui, size=10, weight="bold")
        self.f_mono = tkfont.Font(family=mono, size=9)

    def _build_style(self) -> None:
        P = PALETTE
        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass

        s.configure(".", background=P["bg"], foreground=P["text"],
                    borderwidth=0, font=self.f_ui)
        s.configure("TFrame", background=P["bg"])
        s.configure("Card.TFrame", background=P["card"])
        s.configure("TLabel", background=P["bg"], foreground=P["text"])
        s.configure("Card.TLabel", background=P["card"], foreground=P["text"])
        s.configure("Muted.TLabel", background=P["card"], foreground=P["muted"])
        s.configure("Title.TLabel", background=P["card"], foreground=P["text"],
                    font=self.f_ui_bold)
        s.configure("Section.TLabel", background=P["card"], foreground=P["muted"],
                    font=self.f_ui_bold)

        for name, bg, dark in (("Primary.TButton", P["accent"], P["accent_dark"]),
                               ("Danger.TButton", P["danger"], P["danger_dark"]),
                               ("Tool.TButton", P["card"], P["head_bg"])):
            s.configure(name, background=bg, foreground=P["card"] if bg != P["card"] else P["text"],
                        borderwidth=0, focuscolor=bg, padding=(12, 6))
            s.map(name,
                  background=[("disabled", bg), ("pressed", dark), ("active", dark)],
                  foreground=[("disabled", P["disable_fg"])])

        s.configure("ToolSmall.TButton", background=P["card"], foreground=P["text"],
                    borderwidth=0, focuscolor=P["card"], padding=(8, 4))
        s.map("ToolSmall.TButton",
              background=[("disabled", P["card"]), ("pressed", P["head_bg"]),
                          ("active", P["head_bg"])],
              foreground=[("disabled", P["disable_fg"])])

        s.configure("Treeview", background=P["card"], fieldbackground=P["card"],
                    foreground=P["text"], rowheight=30, borderwidth=0)
        s.configure("Treeview.Heading", background=P["head_bg"], foreground=P["muted"],
                    relief="flat", borderwidth=0, padding=(8, 8))
        s.map("Treeview.Heading", background=[("active", P["head_bg"])])
        s.map("Treeview", background=[("selected", P["accent_soft"])],
              foreground=[("selected", P["accent_dark"])])

        s.configure("Vertical.TScrollbar", background=P["head_bg"], troughcolor=P["card"],
                    bordercolor=P["card"], arrowcolor=P["muted"], relief="flat")
        s.map("Vertical.TScrollbar", background=[("active", P["border"])])

        s.configure("Accent.Horizontal.TProgressbar", troughcolor=P["head_bg"],
                    background=P["accent"], borderwidth=0, thickness=8)

        s.configure("TEntry", fieldbackground=P["card"], foreground=P["text"],
                    bordercolor=P["border"], lightcolor=P["border"], darkcolor=P["border"],
                    insertcolor=P["text"], padding=5)
        s.map("TEntry", bordercolor=[("focus", P["accent"])],
              lightcolor=[("focus", P["accent"])], darkcolor=[("focus", P["accent"])])
        s.configure("TCombobox", fieldbackground=P["card"], foreground=P["text"],
                    bordercolor=P["border"], lightcolor=P["border"], darkcolor=P["border"],
                    arrowcolor=P["muted"], padding=4)
        s.map("TCombobox", bordercolor=[("focus", P["accent"])])

        s.configure("Card.TCheckbutton", background=P["card"], foreground=P["text"],
                    focuscolor=P["card"], padding=(0, 3))
        s.map("Card.TCheckbutton", background=[("active", P["card"])])

        s.configure("Card.TLabelframe", background=P["card"], bordercolor=P["border"],
                    relief="solid", borderwidth=1, padding=10)
        s.configure("Card.TLabelframe.Label", background=P["bg"], foreground=P["muted"])
        s.configure("TSeparator", background=P["border"])

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_menu(self) -> None:
        m = tk.Menu(self)
        file_menu = tk.Menu(m, tearoff=False)
        file_menu.add_command(label="打开配置…", command=self.open_profile, accelerator="Ctrl+O")
        file_menu.add_command(label="保存配置", command=self.save_profile, accelerator="Ctrl+S")
        file_menu.add_command(label="另存配置为…", command=self.save_profile_as)
        file_menu.add_separator()
        file_menu.add_command(label="新建空白配置", command=self.new_profile)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.destroy)
        m.add_cascade(label="文件", menu=file_menu)

        tool_menu = tk.Menu(m, tearoff=False)
        tool_menu.add_command(label="设置 adb/fastboot 路径…", command=self.open_settings)
        tool_menu.add_command(label="固件导出（备份）…", command=self.open_export)
        tool_menu.add_command(label="打开日志目录", command=lambda: self._open_dir(app_root() / "logs"))
        tool_menu.add_command(label="打开配置目录", command=lambda: self._open_dir(app_root() / "config"))
        m.add_cascade(label="工具", menu=tool_menu)

        help_menu = tk.Menu(m, tearoff=False)
        help_menu.add_command(label="关于", command=show_about)
        m.add_cascade(label="帮助", menu=help_menu)
        self.config(menu=m)
        self.bind("<Control-o>", lambda e: self.open_profile())
        self.bind("<Control-s>", lambda e: self.save_profile())

    def _build_header(self) -> None:
        P = PALETTE
        header = tk.Frame(self, bg=P["header"], height=58)
        header.pack(fill="x")
        header.pack_propagate(False)

        left = tk.Frame(header, bg=P["header"])
        left.pack(side="left", padx=18)
        tk.Label(left, text=f"⚡ {APP_NAME}", bg=P["header"], fg=P["header_fg"],
                 font=self.f_title).pack(side="left")
        tk.Label(left, text="MSM8916 刷机与模块配置工具", bg=P["header"],
                 fg=P["header_sub"], font=self.f_ui).pack(side="left", padx=(12, 0), pady=(4, 0))

        self.device_chip = tk.Label(header, text="  检测中…  ", bg=P["log_sb"],
                                    fg=P["header_fg"], font=self.f_chip, padx=6)
        self.device_chip.pack(side="right", padx=18, pady=14)

    def _build_toolbar(self) -> None:
        P = PALETTE
        bar = ttk.Frame(self, padding=(14, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="配置档案", style="TLabel").pack(side="left", padx=(0, 8))
        box = tk.Frame(bar, bg=P["border"], highlightthickness=1,
                       highlightbackground=P["border"])
        box.pack(side="left")
        self.profile_var = tk.StringVar(value="（未加载 — 文件→打开配置 或 新建）")
        tk.Label(box, textvariable=self.profile_var, bg=P["card"], fg=P["text"],
                 font=self.f_ui, padx=12, pady=5, width=42, anchor="w").pack()
        for text, cmd in (("打开…", self.open_profile), ("保存", self.save_profile),
                          ("另存为…", self.save_profile_as), ("新建", self.new_profile)):
            ttk.Button(bar, text=text, style="Tool.TButton", command=cmd).pack(
                side="left", padx=(8, 0))
        ttk.Label(bar, text="镜像目录 = 配置文件目录", style="TLabel",
                  foreground=P["muted"]).pack(side="left", padx=(16, 0))

    def _build_body(self) -> None:
        P = PALETTE
        body = ttk.Frame(self, padding=(14, 0))
        body.pack(fill="both", expand=True)

        # ---------- 左侧卡片 ----------
        left = ttk.Labelframe(body, text="设备与选项", style="Card.TLabelframe")
        left.pack(side="left", fill="y", padx=(0, 12), pady=(0, 2))

        inner = ttk.Frame(left, style="Card.TFrame")
        inner.pack(fill="both", expand=True)
        self._left_inner = inner

        ttk.Label(inner, text="当前设备", style="Muted.TLabel").pack(anchor="w")
        self.device_chip2 = tk.Label(inner, text="检测中…", bg=P["log_sb"], fg=P["header_fg"],
                                     font=self.f_chip, padx=10, pady=5)
        self.device_chip2.pack(anchor="w", pady=(6, 4))
        self.device_detail = tk.Label(inner, text="", bg=P["card"], fg=P["muted"],
                                      font=self.f_ui, wraplength=200, justify="left")
        self.device_detail.pack(anchor="w", pady=(0, 8))

        ttk.Button(inner, text="立即刷新", style="Tool.TButton",
                   command=self._poll_device_now).pack(fill="x", pady=2)
        ttk.Button(inner, text="重启到 Bootloader", style="Tool.TButton",
                   command=self.reboot_to_bootloader).pack(fill="x", pady=2)
        ttk.Button(inner, text="⬇ 导出固件（备份）…", style="Tool.TButton",
                   command=self.open_export).pack(fill="x", pady=2)
        self.btn_refresh, self.btn_to_bl, self.btn_export = inner.winfo_children()[-3:]

        ttk.Separator(inner, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(inner, text="刷机选项", style="Muted.TLabel").pack(anchor="w")
        self.opt_reboot = tk.BooleanVar(value=self.settings.options.reboot_after)
        ttk.Checkbutton(inner, text="完成后重启设备", style="Card.TCheckbutton",
                        variable=self.opt_reboot).pack(anchor="w", pady=1)
        self.opt_sha = tk.BooleanVar(value=self.settings.options.verify_sha256)
        ttk.Checkbutton(inner, text="刷写前校验 SHA256（若配置）", style="Card.TCheckbutton",
                        variable=self.opt_sha).pack(anchor="w", pady=1)

        ttk.Separator(inner, orient="horizontal").pack(fill="x", pady=10)
        ttk.Label(inner, text="平台工具", style="Muted.TLabel").pack(anchor="w")
        self.adb_lbl = tk.Label(inner, text="adb：检测中", bg=P["card"], fg=P["muted"],
                                font=self.f_ui, anchor="w")
        self.adb_lbl.pack(fill="x", pady=(4, 0))
        self.fb_lbl = tk.Label(inner, text="fastboot：检测中", bg=P["card"], fg=P["muted"],
                               font=self.f_ui, anchor="w")
        self.fb_lbl.pack(fill="x")
        self.btn_tools = ttk.Button(inner, text="设置工具路径…", style="Tool.TButton",
                                    command=self.open_settings)
        self.btn_tools.pack(fill="x", pady=(6, 0))

        self.progress = ttk.Progressbar(inner, mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(14, 6))
        self.btn_preview = ttk.Button(inner, text="① 生成预览", style="Primary.TButton",
                                      command=self.preview_plan)
        self.btn_preview.pack(fill="x", pady=3)
        self.btn_flash = ttk.Button(inner, text="② 开始刷机…", style="Danger.TButton",
                                    command=self.start_flash)
        self.btn_flash.pack(fill="x", pady=3)
        self.btn_cancel = ttk.Button(inner, text="取消执行", style="Tool.TButton",
                                     command=self.cancel_flash, state="disabled")
        self.btn_cancel.pack(fill="x", pady=3)

        # ---------- 右侧 ----------
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True)

        mod_card = ttk.Labelframe(right, text="模块（每行 = 一个分区；双击编辑）",
                                  style="Card.TLabelframe")
        mod_card.pack(fill="both", expand=True)
        cols = ("name", "partition", "image", "status")

        btns = ttk.Frame(mod_card, style="Card.TFrame")
        btns.pack(side="bottom", fill="x", pady=(8, 0))
        for text, cmd in (("＋ 添加", self.add_module), ("✎ 编辑", self.edit_module),
                          ("⧉ 复制", self.duplicate_module), ("✕ 删除", self.delete_module),
                          ("↑ 上移", lambda: self.move_module(-1)),
                          ("↓ 下移", lambda: self.move_module(1))):
            ttk.Button(btns, text=text, style="ToolSmall.TButton", command=cmd,
                       width=8).pack(side="left", padx=(0, 6))
        self._module_buttons = list(btns.winfo_children())

        self.tree = ttk.Treeview(mod_card, columns=cols, show="tree headings",
                                 height=7, selectmode="browse", style="Treeview")
        self.tree.heading("#0", text="启用")
        self.tree.column("#0", width=56, anchor="center", stretch=False)
        self.tree.heading("name", text="模块名称")
        self.tree.column("name", width=150, anchor="w")
        self.tree.heading("partition", text="分区")
        self.tree.column("partition", width=100, anchor="w")
        self.tree.heading("image", text="镜像文件")
        self.tree.column("image", width=270, anchor="w")
        self.tree.heading("status", text="状态")
        self.tree.column("status", width=190, anchor="w")
        vsb = ttk.Scrollbar(mod_card, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(2, 0))
        vsb.pack(side="left", fill="y")
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", lambda e: self.edit_module())
        self.tree.bind("<space>", lambda e: self.toggle_selected())
        self.tree.tag_configure("stripe", background=P["row_alt"])
        self.tree.tag_configure("missing", foreground=P["danger"])
        self.tree.tag_configure("danger", foreground=P["warn"])
        self.tree.tag_configure("disabled_row", foreground="#9ca3af")

        log_card = ttk.Labelframe(right, text="日志", style="Card.TLabelframe")
        log_card.pack(fill="both", expand=True, pady=(12, 2))
        log_wrap = tk.Frame(log_card, bg=P["log_bg"])
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_wrap, height=7, state="disabled", bd=0,
                                bg=P["log_bg"], fg=P["log_fg"], font=self.f_mono,
                                insertbackground=P["log_fg"], selectbackground="#334155",
                                padx=10, pady=8, wrap="none")
        sb = tk.Scrollbar(log_wrap, command=self.log_text.yview, bg=P["log_sb"],
                          troughcolor=P["log_bg"], activebackground="#334155",
                          bd=0, elementborderwidth=0, width=10)
        self.log_text.configure(yscrollcommand=sb.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        for tag, color in (("err", "#f87171"), ("warn", "#fbbf24"), ("ok", "#34d399")):
            self.log_text.tag_configure(tag, foreground=color)

    def _build_statusbar(self) -> None:
        P = PALETTE
        bar = tk.Frame(self, bg=P["card"], highlightbackground=P["border"],
                       highlightthickness=1)
        bar.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="就绪")
        tk.Label(bar, textvariable=self.status_var, bg=P["card"], fg=P["muted"],
                 font=self.f_ui, anchor="w", padx=14, pady=5).pack(fill="x")

    # ==================================================================
    # 设备轮询
    # ==================================================================
    def _device_chip_colors(self, info: DeviceInfo) -> tuple[str, str]:
        P = PALETTE
        if info.mode == "bootloader":
            return P["ok_soft"], P["ok_deep"]
        if info.mode == "system":
            return P["accent_soft"], "#1e40af"
        if not (info.adb_found or info.fastboot_found):
            return P["danger_soft"], "#991b1b"
        return P["log_sb"], P["header_fg"]

    def _poll_device_now(self) -> None:
        self._poll_device()

    def _poll_device(self) -> None:
        P = PALETTE
        if self.executor is None:  # 刷机中不轮询，避免干扰 fastboot
            info = self.device.probe()
            chip_bg, chip_fg = self._device_chip_colors(info)
            text = "⚠ 未找到 adb / fastboot 工具" if info.mode == "none" and \
                not (info.adb_found or info.fastboot_found) else info.label
            for chip in (self.device_chip, self.device_chip2):
                chip.configure(text=f"  {text}  ", bg=chip_bg, fg=chip_fg)
            self.device_detail.configure(text=info.detail or "等待设备接入")
            self.adb_lbl.configure(
                text=f"adb：{'✓ 已就绪' if info.adb_found else '✗ 未找到'}",
                fg=P["ok"] if info.adb_found else "#9ca3af")
            self.fb_lbl.configure(
                text=f"fastboot：{'✓ 已就绪' if info.fastboot_found else '✗ 未找到'}",
                fg=P["ok"] if info.fastboot_found else "#9ca3af")
        self.after(POLL_MS, self._poll_device)

    def reboot_to_bootloader(self) -> None:
        if not messagebox.askyesno("确认", "确定将设备重启到 Bootloader (fastboot) 模式？"):
            return
        self.log("→ 正在重启到 Bootloader…")
        ok = self.device.reboot_to_bootloader()
        self.log("✓ 已发送重启命令，请等待设备进入 fastboot" if ok
                 else "✗ 失败：设备需处于系统模式（adb 可见）或已在 Bootloader 模式",
                 "ok" if ok else "err")
        self.after(4000, self._poll_device_now)

    def open_export(self) -> None:
        if self.executor and self.executor.running:
            messagebox.showwarning("刷机进行中", "请等待当前刷机任务结束后再导出。")
            return
        from rustflashkit.gui.export_dialog import ExportDialog
        ExportDialog(self)

    # ==================================================================
    # 配置档案
    # ==================================================================
    def _load_initial_profile(self) -> None:
        last = self.settings.last_profile
        if last and Path(last).is_file():
            try:
                self._apply_profile(Path(last))
            except Exception as exc:
                self.log(f"上次配置加载失败: {exc}", "warn")

    def _apply_profile(self, path: Path) -> None:
        self.profile = Profile.load(path)
        self.profile_path = path
        self.base_dir = path.parent
        self.profile_var.set(f"{path.name}")
        self.settings.last_profile = str(path)
        self.settings.save()
        self.refresh_modules()
        self.log(f"已加载配置: {path}", "ok")

    def open_profile(self) -> None:
        fn = filedialog.askopenfilename(title="打开配置档案",
                                        filetypes=[("Profile JSON", "*.json"), ("所有文件", "*.*")])
        if fn:
            try:
                self._apply_profile(Path(fn))
            except Exception as exc:
                messagebox.showerror("打开失败", f"无法加载配置:\n{exc}")

    def save_profile(self) -> None:
        if self.profile is None:
            messagebox.showinfo("提示", "当前没有已加载的配置，请使用“另存为”。")
            return
        if self.profile_path is None:
            self.save_profile_as()
            return
        try:
            self.profile.save(self.profile_path)
            self.log(f"已保存: {self.profile_path}", "ok")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))

    def save_profile_as(self) -> None:
        if self.profile is None:
            messagebox.showinfo("提示", "没有可保存的配置。")
            return
        fn = filedialog.asksaveasfilename(title="另存配置档案",
                                          defaultextension=".json",
                                          filetypes=[("Profile JSON", "*.json")])
        if not fn:
            return
        self.profile_path = Path(fn)
        self.base_dir = self.profile_path.parent
        self.save_profile()
        self._apply_profile(self.profile_path)

    def new_profile(self) -> None:
        from rustflashkit.gui.dialogs import ask_profile_name
        name = ask_profile_name(self)
        if not name:
            return
        self.profile = Profile(name=name, board="", description="新建空白配置")
        self.profile_path = None
        self.base_dir = app_root()
        self.profile_var.set(f"{name}（未保存）")
        self.refresh_modules()
        self.log(f"已新建空白配置「{name}」，添加模块后请另存。")

    # ==================================================================
    # 模块表格
    # ==================================================================
    def refresh_modules(self) -> None:
        self.tree.delete(*self.tree.get_children())
        if self.profile is None:
            return
        from rustflashkit.core.safety import assess
        for i, mod in enumerate(self.profile.modules):
            exists = mod.image_exists(self.base_dir)
            lvl = assess(mod.partition)
            marks = []
            if not mod.image.strip():
                status = "未设置镜像"
            elif exists:
                size = mod.image_path(self.base_dir).stat().st_size
                status = f"✓ {_fmt_size(size)}"
            elif mod.allow_missing:
                status = "缺失（将跳过）"
            else:
                status = "✗ 镜像缺失"
            if lvl == "bootloader":
                marks.append("⚠引导链")
            elif lvl == "efs":
                marks.append("⛔EFS·禁止")
            if marks:
                status += "  " + " ".join(marks)
            check = "🔒" if mod.required else ("☑" if mod.enabled else "☐")
            tags = []
            if i % 2 == 1:
                tags.append("stripe")
            if lvl == "bootloader":
                tags.append("danger")
            elif lvl == "efs" or (mod.image and not exists and not mod.allow_missing):
                tags.append("missing")
            if not mod.enabled:
                tags.append("disabled_row")
            self.tree.insert("", "end", iid=mod.id,
                             text=check, values=(mod.name, mod.partition,
                                                 mod.image or "—", status),
                             tags=tuple(tags))

    def _selected_id(self) -> Optional[str]:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _on_tree_click(self, event) -> None:
        region = self.tree.identify_region(event.x, event.y)
        if region not in ("tree", "cell"):
            return
        col = self.tree.identify_column(event.x)
        iid = self.tree.identify_row(event.y)
        if iid and (col == "#0" or region == "tree"):
            self.tree.selection_set(iid)
            self._toggle_module(iid)

    def toggle_selected(self) -> None:
        iid = self._selected_id()
        if iid:
            self._toggle_module(iid)

    def _toggle_module(self, iid: str) -> None:
        if self.profile is None:
            return
        mod = next((m for m in self.profile.modules if m.id == iid), None)
        if mod is None or mod.required:
            if mod and mod.required:
                self.status_var.set(f"模块「{mod.name}」为必需项，不能停用")
            return
        mod.enabled = not mod.enabled
        self.refresh_modules()

    def _require_profile(self) -> bool:
        if self.profile is None:
            messagebox.showinfo("提示", "请先打开或新建一个配置档案。")
            return False
        return True

    def add_module(self) -> None:
        if not self._require_profile():
            return
        existing = {m.id for m in self.profile.modules}
        dlg = ModuleEditor(self, base_dir=self.base_dir, used_ids=existing)
        self.wait_window(dlg)
        if dlg.result:
            self.profile.modules.append(dlg.result)
            self.refresh_modules()

    def edit_module(self) -> None:
        iid = self._selected_id()
        if not iid or not self._require_profile():
            return
        mod = next(m for m in self.profile.modules if m.id == iid)
        others = {m.id for m in self.profile.modules} - {mod.id}
        dlg = ModuleEditor(self, module=mod, base_dir=self.base_dir, used_ids=others)
        self.wait_window(dlg)
        if dlg.result:
            idx = self.profile.modules.index(mod)
            self.profile.modules[idx] = dlg.result
            self.refresh_modules()

    def duplicate_module(self) -> None:
        iid = self._selected_id()
        if not iid or not self._require_profile():
            return
        mod = next(m for m in self.profile.modules if m.id == iid)
        import copy
        new = copy.deepcopy(mod)
        n = 2
        while f"{mod.id}-copy{n}" in {m.id for m in self.profile.modules}:
            n += 1
        new.id = f"{mod.id}-copy{n}"
        new.name = f"{mod.name} 副本"
        new.required = False
        self.profile.modules.append(new)
        self.refresh_modules()

    def delete_module(self) -> None:
        iid = self._selected_id()
        if not iid or not self._require_profile():
            return
        mod = next(m for m in self.profile.modules if m.id == iid)
        if mod.required and len(self.profile.modules) > 1:
            messagebox.showwarning("不能删除", "必需模块不能删除（可先取消其“必需”属性）。")
            return
        if messagebox.askyesno("删除模块", f"确定删除模块「{mod.name}」？"):
            self.profile.modules.remove(mod)
            self.refresh_modules()

    def move_module(self, delta: int) -> None:
        iid = self._selected_id()
        if not iid or not self._require_profile():
            return
        mods = self.profile.modules
        idx = next(i for i, m in enumerate(mods) if m.id == iid)
        new_idx = idx + delta
        if 0 <= new_idx < len(mods):
            mods[idx], mods[new_idx] = mods[new_idx], mods[idx]
            self.refresh_modules()
            self.tree.selection_set(iid)

    # ==================================================================
    # 计划与执行
    # ==================================================================
    def _plan_options(self) -> PlanOptions:
        return PlanOptions(reboot_after=self.opt_reboot.get(),
                           verify_sha256=self.opt_sha.get())

    def _build_plan_safe(self):
        return build_plan(self.profile, self.base_dir, self._plan_options())

    def preview_plan(self) -> None:
        if not self._require_profile():
            return
        plan = self._build_plan_safe()
        dlg = PlanPreviewDialog(self, plan)
        self.wait_window(dlg)
        if dlg.confirmed:
            self._launch(plan)

    def start_flash(self) -> None:
        if not self._require_profile():
            return
        if self._export_open:
            messagebox.showwarning("导出窗口已打开",
                                   "导出与刷写不能同时进行，请先关闭固件导出窗口。")
            return
        info = self.device.probe()
        if info.mode == "none":
            messagebox.showwarning(
                "未检测到设备",
                "未检测到 fastboot/adb 设备。\n\n"
                "请将棒子进入 Bootloader 模式后再刷写：\n"
                "  · 已能 adb 连接：点左侧“重启到 Bootloader”\n"
                "  · 无法进系统：断电后按住设备功能键（如有）插入 USB")
            return
        if info.mode == "system":
            if not messagebox.askyesno(
                    "设备在系统模式",
                    "设备当前处于系统 (adb) 模式，刷写需要在 Bootloader 模式进行。\n\n"
                    "现在重启到 Bootloader 并继续吗？"):
                return
            self.log("→ 正在重启到 Bootloader…")
            if not self.device.reboot_to_bootloader():
                messagebox.showerror("失败", "重启到 Bootloader 失败，请手动进入后重试。")
                return
        plan = self._build_plan_safe()
        dlg = PlanPreviewDialog(self, plan)
        self.wait_window(dlg)
        if dlg.confirmed:
            self._launch(plan)

    def _launch(self, plan) -> None:
        if not plan.ok:
            messagebox.showerror("计划有问题", "请先解决预览中列出的问题再执行。")
            return
        self.session_log = SessionLog()
        self.session_log.start()
        self._set_flashing(True)
        self.progress.configure(maximum=len(plan.steps), value=0)
        self.executor = FlashExecutor(
            plan, self.runner, self.device,
            on_log=lambda line: self.ui_queue.put(("log", line)),
            on_step=lambda r: self.ui_queue.put(("step", r)),
            on_done=lambda ok, msg: self.ui_queue.put(("done", (ok, msg))))
        self.executor.start()

    def cancel_flash(self) -> None:
        if self.executor and self.executor.running:
            self.executor.cancel()

    def _set_flashing(self, flashing: bool) -> None:
        state = "disabled" if flashing else "normal"
        for b in [self.btn_preview, self.btn_flash, self.btn_refresh, self.btn_to_bl,
                  self.btn_export, self.btn_tools, *self._module_buttons]:
            b.configure(state=state)
        self.tree.configure(selectmode="none" if flashing else "browse")
        self.btn_cancel.configure(state="normal" if flashing else "disabled")
        self.status_var.set("正在刷机…请勿拔出设备" if flashing else "就绪")

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "step":
                    self._on_step(payload)
                elif kind == "done":
                    self._on_done(*payload)
        except queue.Empty:
            pass
        self.after(80, self._drain_queue)

    def _on_step(self, result: StepResult) -> None:
        self.progress.step()
        mark = "✓" if result.ok else "✗"
        line = f"{mark} [{result.index + 1}/{len(self.executor.plan.steps)}] {result.step.title}"
        if result.detail:
            line += f" — {result.detail}"
        self._append_log(line, "ok" if result.ok else "err")

    def _on_done(self, ok: bool, message: str) -> None:
        self._set_flashing(False)
        self.executor = None
        if self.session_log:
            self.session_log.write(f"== 结束: {'成功' if ok else '失败/取消'} — {message}")
            self.session_log.close()
        self._append_log(("✔ 刷机完成: " if ok else "✖ 刷机未完成: ") + message,
                         "ok" if ok else "err")
        self.log(f"会话日志: {self.session_log.path if self.session_log else ''}")
        if ok:
            messagebox.showinfo("完成", f"刷机完成。\n{message}")
        else:
            messagebox.showerror("未完成", f"刷机未完成：\n{message}\n\n请查看日志排查。")

    # ==================================================================
    # 其他
    # ==================================================================
    def open_settings(self) -> None:
        dlg = SettingsDialog(self, self.settings, on_saved=self._tools_changed)
        self.wait_window(dlg)
        self._tools_changed()

    def _tools_changed(self) -> None:
        self.locator.adb_path = self.settings.adb_path
        self.locator.fastboot_path = self.settings.fastboot_path
        self._poll_device_now()

    def _open_dir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        import subprocess, sys
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])

    def log(self, line: str, tag: str = "") -> None:
        self.ui_queue.put(("log", (line, tag)))

    def _append_log(self, payload, default_tag: str = "") -> None:
        if isinstance(payload, tuple):
            line, tag = payload
        else:
            line, tag = payload, default_tag
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line.rstrip("\n") + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        if self.session_log:
            self.session_log.write(line)

    def on_closing(self) -> None:
        if self.executor and self.executor.running:
            if not messagebox.askyesno("刷机进行中", "刷机正在进行，确定要退出吗？\n退出可能使设备处于中间状态。"):
                return
            self.executor.cancel()
        self.settings.options.reboot_after = self.opt_reboot.get()
        self.settings.options.verify_sha256 = self.opt_sha.get()
        self.settings.save()
        self.destroy()


def _fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def main() -> None:
    app = MainApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
