"""固件导出（备份）对话框：读取分区表 → 多选导出 → （可选）生成配置档案。

导出为只读操作；需要设备处于系统模式（adb 可见）。
"""
from __future__ import annotations

import queue
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from rustflashkit.core.exporter import (ExportJob, ExportItemResult,
                                        PartitionInfo, export_timeout,
                                        read_partition_table)
from rustflashkit.core.modules import FlashModule, Profile, MODULE_ID_RE
from rustflashkit.core.runner import app_root
from rustflashkit.gui.theme import PALETTE as P

PRESET_PARTS = {"boot", "rootfs", "system", "recovery", "nshstate", "splash", "modem"}


class ExportDialog(tk.Toplevel):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.title("固件导出 / 备份")
        self.geometry("820x660")
        self.transient(main)
        self.grab_set()
        self.configure(bg=P["bg"])
        self.f_ui, self.f_mono, self.f_bold = main.f_ui, main.f_mono, main.f_ui_bold

        self.runner = main.runner
        self.job: Optional[ExportJob] = None
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.parts: list[PartitionInfo] = []
        self.results: dict[str, ExportItemResult] = {}
        self.out_dir: Path = app_root() / "exports"
        main._export_open = True

        self._build_style_extras()
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._drain)
        self.after(200, self._refresh_device)

    # ------------------------------------------------------------------
    def _build_style_extras(self) -> None:
        s = ttk.Style(self)
        s.configure("TFrame", background=P["bg"])
        s.configure("TLabel", background=P["bg"], foreground=P["text"])
        s.configure("Muted.TLabel", background=P["card"], foreground=P["muted"])
        s.configure("Title.TLabel", background=P["card"], foreground=P["text"],
                    font=self.f_bold)

    def _build_ui(self) -> None:
        wrap = ttk.Frame(self, padding=(14, 12))
        wrap.pack(fill="both", expand=True)

        # 顶栏：标题 + 设备徽章
        top = ttk.Frame(wrap)
        top.pack(fill="x")
        ttk.Label(top, text="⬇ 固件导出 / 备份", font=self.f_bold).pack(side="left")
        self.chip = tk.Label(top, text="  检测中…  ", bg=P["log_sb"], fg=P["header_fg"],
                             font=self.f_bold, padx=6)
        self.chip.pack(side="right")

        # ① 分区表
        c1 = ttk.Labelframe(wrap, text=" ① 选择要导出的分区 ", style="Card.TLabelframe")
        c1.pack(fill="both", expand=True, pady=(10, 0))

        btns = ttk.Frame(c1, style="Card.TFrame")
        btns.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_read = ttk.Button(btns, text="读取分区表", style="Primary.TButton",
                                   command=self._read_table)
        self.btn_read.pack(side="left")
        for text, cmd in (("全选", lambda: self._select_all(True)),
                          ("全不选", lambda: self._select_all(False)),
                          ("常用分区", self._select_preset)):
            ttk.Button(btns, text=text, style="ToolSmall.TButton", width=8,
                       command=cmd).pack(side="left", padx=(8, 0))
        ttk.Label(btns, text="Ctrl/Shift 多选 · 导出为只读操作",
                  style="Muted.TLabel").pack(side="left", padx=(14, 0))

        cols = ("size", "path")
        self.tree = ttk.Treeview(c1, columns=cols, show="tree headings",
                                 height=7, selectmode="extended", style="Treeview")
        self.tree.heading("#0", text="分区")
        self.tree.column("#0", width=130, anchor="w")
        self.tree.heading("size", text="大小")
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.heading("path", text="设备节点")
        self.tree.column("path", width=300, anchor="w")
        vsb = ttk.Scrollbar(c1, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=(2, 0))
        vsb.pack(side="left", fill="y", pady=(2, 0))
        self.tree.tag_configure("stripe", background=P["row_alt"])
        self.tree.tag_configure("done_ok", foreground=P["ok"])
        self.tree.tag_configure("done_fail", foreground=P["danger"])

        # ② 导出设置
        c2 = ttk.Labelframe(wrap, text=" ② 导出设置 ", style="Card.TLabelframe")
        c2.pack(fill="x", pady=(12, 0))
        row = ttk.Frame(c2, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="输出目录", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.v_out = tk.StringVar(value=str(self.out_dir))
        ttk.Entry(row, textvariable=self.v_out).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="浏览…", style="ToolSmall.TButton", width=7,
                   command=self._browse_out).pack(side="left", padx=(6, 0))
        row2 = ttk.Frame(c2, style="Card.TFrame")
        row2.pack(fill="x", pady=(8, 0))
        self.v_sha = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="导出后计算 SHA256（用于配置档案刷前校验）",
                        style="Card.TCheckbutton", variable=self.v_sha).pack(side="left")
        self.progress = ttk.Progressbar(row2, mode="determinate",
                                        style="Accent.Horizontal.TProgressbar")
        self.progress.pack(side="right", fill="x", expand=True, padx=(16, 0))

        actions = ttk.Frame(c2, style="Card.TFrame")
        actions.pack(fill="x", pady=(10, 0))
        self.btn_start = ttk.Button(actions, text="开始导出", style="Primary.TButton",
                                    command=self._start, state="disabled")
        self.btn_start.pack(side="left")
        self.btn_cancel = ttk.Button(actions, text="取消导出", style="Tool.TButton",
                                     command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", padx=(8, 0))
        self.btn_profile = ttk.Button(actions, text="③ 从导出生成配置档案…",
                                      style="Tool.TButton", command=self._gen_profile,
                                      state="disabled")
        self.btn_profile.pack(side="right")

        # 日志
        c3 = ttk.Labelframe(wrap, text=" 日志 ", style="Card.TLabelframe")
        c3.pack(fill="both", expand=True, pady=(12, 0))
        log_wrap = tk.Frame(c3, bg=P["log_bg"])
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_wrap, height=6, state="disabled", bd=0,
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

    # ==================================================================
    # 设备与分区表
    # ==================================================================
    def _refresh_device(self) -> None:
        dev = self.main.current_device() or self.main.device.probe()
        if dev is None or dev.mode == "none":
            bg, fg, text = P["danger_soft"], "#991b1b", "⚠ 未检测到设备"
        elif dev.mode == "system":
            bg, fg, text = P["accent_soft"], "#1e40af", dev.label
        elif dev.mode == "bootloader":
            bg, fg, text = P["warn_soft"], P["warn"], f"{dev.label}（导出需系统模式 adb）"
        else:
            bg, fg, text = P["log_sb"], P["header_fg"], dev.label
        self.chip.configure(text=f"  {text}  ", bg=bg, fg=fg)

    def _read_table(self) -> None:
        dev = self.main.current_device() or self.main.device.probe()
        self._refresh_device()
        if dev is None or dev.mode != "system":
            messagebox.showwarning(
                "需要 adb 设备",
                "固件导出需要设备处于系统模式（adb 可见）。\n\n"
                "Bootloader (fastboot) 模式没有标准的分区读取命令。\n"
                "若设备当前在 Bootloader，请重启回系统后再试。")
            return
        serial = dev.serial
        self._log(f"→ 正在读取分区表（{serial}）…")
        try:
            self.parts = read_partition_table(self.runner, serial)
        except RuntimeError as exc:
            self._log(f"✗ {exc}", "err")
            messagebox.showerror("读取失败", str(exc))
            return
        self._log(f"✓ 共 {len(self.parts)} 个分区", "ok")
        self.tree.delete(*self.tree.get_children())
        for i, pt in enumerate(self.parts):
            tags = ["stripe"] if i % 2 else []
            self.tree.insert("", "end", iid=pt.name, text=pt.name,
                             values=(pt.size_text, pt.path), tags=tuple(tags))
        self.btn_start.configure(state="normal")
        self._select_preset(silent=True)

    def _select_all(self, on: bool) -> None:
        if on:
            self.tree.selection_set(self.tree.get_children())
        else:
            self.tree.selection_remove(*self.tree.selection())

    def _select_preset(self, silent: bool = False) -> None:
        preset_ids = [pt.name for pt in self.parts
                      if pt.name.lower() in PRESET_PARTS and pt.size > 0]
        self.tree.selection_set(preset_ids)
        if not silent:
            self._log("已选中常用分区（boot/rootfs/system/recovery/NSHSTATE 等），可自行增删")

    # ==================================================================
    # 导出执行
    # ==================================================================
    def _selected_parts(self) -> list[PartitionInfo]:
        sel = set(self.tree.selection())
        return [pt for pt in self.parts if pt.name in sel]

    def _browse_out(self) -> None:
        d = filedialog.askdirectory(title="选择导出目录", initialdir=str(self.out_dir))
        if d:
            self.out_dir = Path(d)
            self.v_out.set(d)

    def _start(self) -> None:
        if self.job and self.job.running:
            return
        parts = self._selected_parts()
        if not parts:
            messagebox.showinfo("提示", "请先在列表中选择要导出的分区。")
            return
        if any(not MODULE_ID_RE.match(p.name.lower()) for p in parts):
            messagebox.showwarning("异常分区名",
                                   "存在无法用于配置档案的分区名（含特殊字符），请检查选择。")
            return
        self.out_dir = Path(self.v_out.get().strip() or (app_root() / "exports"))
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.results.clear()
        self._set_running(True)
        self.progress.configure(maximum=len(parts), value=0)
        dev = self.main.current_device()
        if dev is None or dev.mode != "system":
            self._set_running(False)
            messagebox.showwarning("设备已变化", "目标设备不可用，请重新读取分区表。")
            return
        self.job = ExportJob(
            self.runner, dev.serial, parts, self.out_dir,
            with_sha256=self.v_sha.get(),
            on_log=lambda s: self.q.put(("log", s)),
            on_item=lambda r: self.q.put(("item", r)),
            on_done=lambda ok, msg: self.q.put(("done", (ok, msg))))
        self.job.start()

    def _cancel(self) -> None:
        if self.job and self.job.running:
            self.job.cancel()

    def _set_running(self, running: bool) -> None:
        for b in (self.btn_start, self.btn_read):
            b.configure(state="disabled" if running else "normal")
        self.btn_cancel.configure(state="normal" if running else "disabled")
        if running:
            self.btn_profile.configure(state="disabled")
        else:
            has_ok = any(r.ok for r in self.results.values())
            self.btn_profile.configure(state="normal" if has_ok else "disabled")

    # ------------------------------------------------------------------
    def _gen_profile(self) -> None:
        ok_items = [r for r in self.results.values() if r.ok]
        if not ok_items:
            messagebox.showinfo("提示", "还没有成功导出的分区。")
            return
        prof = Profile(
            name=f"设备备份 {datetime.now():%Y-%m-%d}",
            description=f"由固件导出自动生成（{len(ok_items)} 个分区，含 SHA256）；"
                        "刷写前请逐项核对分区与镜像。")
        for r in sorted(ok_items, key=lambda x: x.part.name.lower()):
            prof.modules.append(FlashModule(
                id=r.part.name.lower(), name=r.part.name,
                partition=r.part.name, image=r.part.name + ".img",
                enabled=True, sha256=r.sha256,
                timeout=max(300, export_timeout(r.part.size))))
        fn = filedialog.asksaveasfilename(
            title="保存配置档案", defaultextension=".json",
            initialdir=str(self.out_dir),
            initialfile="backup_profile.json",
            filetypes=[("Profile JSON", "*.json")])
        if not fn:
            return
        path = Path(fn)
        try:
            prof.save(path)
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc))
            return
        self._log(f"✓ 已生成配置档案: {path}", "ok")
        self.main._apply_profile(path)
        messagebox.showinfo("完成",
                            "配置档案已生成并载入主窗口。\n"
                            "镜像目录即导出目录，可在主窗口勾选后直接刷回。")

    # ==================================================================
    # 队列与日志
    # ==================================================================
    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "item":
                    r = payload
                    self.results[r.part.name] = r
                    self.progress.step()
                    tags = [t for t in self.tree.item(r.part.name, "tags")
                            if t not in ("done_ok", "done_fail")]
                    tags.append("done_ok" if r.ok else "done_fail")
                    self.tree.item(r.part.name, tags=tuple(tags))
                    if not r.ok:
                        self._log(f"✗ {r.part.name}: {r.detail}", "err")
                elif kind == "done":
                    self._on_done(*payload)
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _on_done(self, ok: bool, message: str) -> None:
        self._set_running(False)
        self.job = None
        self._log(("✔ " if ok else "✖ ") + message, "ok" if ok else "err")
        ok_items = [r for r in self.results.values() if r.ok]
        if ok_items:
            self.btn_profile.configure(state="normal")
        if ok:
            messagebox.showinfo("导出完成",
                                f"已导出 {len(ok_items)} 个分区到\n{self.out_dir}")
        else:
            messagebox.showwarning("导出未完成",
                                   f"{message}\n\n已完成的分区仍可用于生成配置档案。")

    def _log(self, payload, tag: str = "") -> None:
        if isinstance(payload, tuple):
            line, tag = payload
        else:
            line, tag = payload, tag
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line.rstrip("\n") + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self.job and self.job.running:
            if not messagebox.askyesno("导出进行中", "导出正在进行，确定取消并关闭？"):
                return
            self.job.cancel()
        self.main._export_open = False
        self.destroy()
