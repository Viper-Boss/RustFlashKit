"""蓝牙（BLE）配置对话框（实验性）。

固件端协议未定稿期间的 BLE 诊断 + 模板化发送骨架。所有 BLE 操作在
工作线程的独立事件循环里执行，结果经队列转回 UI 线程。
"""
from __future__ import annotations

import asyncio
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from rustflashkit.core import ble
from rustflashkit.core.runner import app_root
from rustflashkit.gui.theme import PALETTE as P


class BleDialog(tk.Toplevel):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.title("蓝牙（BLE）配置 — 实验性")
        self.geometry("860x680")
        self.transient(main)
        self.grab_set()
        self.configure(bg=P["bg"])
        self.f_ui, self.f_mono, self.f_bold = main.f_ui, main.f_mono, main.f_ui_bold

        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self.template = None
        self.devices: list[dict] = []

        self._build()
        self.after(80, self._drain)
        self._load_template()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        wrap = ttk.Frame(self, padding=(14, 12))
        wrap.pack(fill="both", expand=True)

        top = ttk.Frame(wrap)
        top.pack(fill="x")
        ttk.Label(top, text="📶 蓝牙（BLE）配置", font=self.f_bold).pack(side="left")
        self.status_chip = tk.Label(top, text="  实验性  ", bg=P["warn_soft"],
                                    fg=P["warn"], font=self.f_bold, padx=6)
        self.status_chip.pack(side="right")

        note = tk.Label(
            wrap, justify="left", anchor="w", wraplength=800, padx=12, pady=8,
            bg=P["warn_soft"], fg=P["text"], font=self.f_ui,
            text="固件端 BLE 应急配网协议尚未定稿（RustSimHub docs/ble 任务书阶段：GATT UUID、"
                 "加密会话均待实施）。本窗口当前可用于：① 探测棒子 BLE 广播与 GATT 能力；"
                 "② 协议定稿后，修改 config/ble-provisioning-template.json 即可对接发送。")
        note.pack(fill="x", pady=(10, 0))

        # ① 扫描
        c1 = ttk.Labelframe(wrap, text=" ① 扫描附近 BLE 设备 ", style="Card.TLabelframe")
        c1.pack(fill="both", expand=True, pady=(10, 0))
        btns1 = ttk.Frame(c1, style="Card.TFrame")
        btns1.pack(side="bottom", fill="x", pady=(8, 0))
        self.btn_scan = ttk.Button(btns1, text="开始扫描（6s）", style="Primary.TButton",
                                   command=self._scan)
        self.btn_scan.pack(side="left")
        ttk.Label(btns1, text="双击设备 → 枚举 GATT 服务", style="Muted.TLabel").pack(
            side="left", padx=(12, 0))
        cols = ("name", "address", "rssi")
        self.tree = ttk.Treeview(c1, columns=cols, show="headings", height=6,
                                 selectmode="browse", style="Treeview")
        for cid, text, w, anchor in (("name", "设备名", 220, "w"),
                                     ("address", "地址", 180, "w"),
                                     ("rssi", "信号", 80, "e")):
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor=anchor)
        sb = ttk.Scrollbar(c1, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True, padx=(2, 0), pady=(2, 0))
        sb.pack(side="left", fill="y", pady=(2, 0))
        self.tree.tag_configure("stripe", background=P["row_alt"])
        self.tree.bind("<Double-1>", lambda e: self._enumerate())

        # ② 配网发送
        c2 = ttk.Labelframe(wrap, text=" ② 配网载荷（模板：config/ble-provisioning-template.json）",
                            style="Card.TLabelframe")
        c2.pack(fill="x", pady=(12, 0))
        grid = ttk.Frame(c2, style="Card.TFrame")
        grid.pack(fill="x")
        self.v_addr = tk.StringVar()
        self.v_ssid = tk.StringVar()
        self.v_psk = tk.StringVar()
        self.v_uuid = tk.StringVar()
        for r, (label, var, w) in enumerate((
                ("目标设备", self.v_addr, 20),
                ("Wi-Fi SSID", self.v_ssid, 24),
                ("Wi-Fi 密码", self.v_psk, 24),
                ("写特征 UUID", self.v_uuid, 38))):
            ttk.Label(grid, text=label, style="Muted.TLabel").grid(
                row=r, column=0, sticky="w", padx=(0, 8), pady=3)
            ttk.Entry(grid, textvariable=var, width=w).grid(
                row=r, column=1, sticky="w", pady=3)
        ttk.Label(grid, text="（密码在本机拼接 JSON 后写入特征）",
                  style="Muted.TLabel").grid(row=0, column=2, padx=(10, 0), sticky="w")
        btns2 = ttk.Frame(c2, style="Card.TFrame")
        btns2.pack(fill="x", pady=(8, 0))
        ttk.Button(btns2, text="选择模板…", style="ToolSmall.TButton", width=10,
                   command=self._pick_template).pack(side="left")
        ttk.Button(btns2, text="预览载荷", style="ToolSmall.TButton", width=10,
                   command=self._preview).pack(side="left", padx=(8, 0))
        self.btn_send = ttk.Button(btns2, text="发送配网载荷", style="Danger.TButton",
                                   command=self._send, state="disabled")
        self.btn_send.pack(side="right")

        # 日志
        c3 = ttk.Labelframe(wrap, text=" 日志 ", style="Card.TLabelframe")
        c3.pack(fill="both", expand=True, pady=(12, 0))
        log_wrap = tk.Frame(c3, bg=P["log_bg"])
        log_wrap.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_wrap, height=6, state="disabled", bd=0,
                                bg=P["log_bg"], fg=P["log_fg"], font=self.f_mono,
                                insertbackground=P["log_fg"], selectbackground="#334155",
                                padx=10, pady=8, wrap="word")
        sb2 = tk.Scrollbar(log_wrap, command=self.log_text.yview, bg=P["log_sb"],
                           troughcolor=P["log_bg"], activebackground="#334155",
                           bd=0, elementborderwidth=0, width=10)
        self.log_text.configure(yscrollcommand=sb2.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb2.pack(side="left", fill="y")
        for tag, color in (("err", "#f87171"), ("warn", "#fbbf24"), ("ok", "#34d399")):
            self.log_text.tag_configure(tag, foreground=color)

    # ==================================================================
    # 模板
    # ==================================================================
    def _load_template(self, path: Optional[Path] = None) -> None:
        path = path or app_root() / "config" / "ble-provisioning-template.json"
        try:
            self.template = ble.ensure_template(path)
        except Exception as exc:
            self._log(f"模板加载失败: {exc}", "err")
            self.template = None
            return
        self.v_uuid.set(self.template.write_char)
        mark = "（占位符）" if self.template.is_placeholder else ""
        self._log(f"模板: {self.template.name} {mark}")
        if self.template.is_placeholder:
            self._log("⚠ 模板 UUID 为全零占位符——发送已禁用，待固件协议定稿后编辑模板", "warn")

    def _pick_template(self) -> None:
        fn = filedialog.askopenfilename(title="选择 BLE 模板",
                                        initialdir=str(app_root() / "config"),
                                        filetypes=[("JSON", "*.json")])
        if fn:
            self._load_template(Path(fn))

    # ==================================================================
    # BLE 动作（线程 + asyncio）
    # ==================================================================
    def _run_async(self, coro_factory, on_done) -> None:
        if self.busy:
            messagebox.showinfo("提示", "当前有 BLE 操作进行中，请稍候。", parent=self)
            return

        def worker():
            try:
                result = asyncio.run(coro_factory())
                self.q.put(("ok", (result, on_done)))
            except ble.BleUnavailable as exc:
                self.q.put(("err", (str(exc), None)))
            except Exception as exc:  # bleak 各种连接错误
                self.q.put(("err", (f"{type(exc).__name__}: {exc}", None)))

        self._set_busy(True)
        threading.Thread(target=worker, daemon=True).start()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.btn_scan.configure(state="disabled" if busy else "normal")
        self.btn_send.configure(state="disabled" if busy or self._template_placeholder()
                                else "normal")

    def _template_placeholder(self) -> bool:
        return self.template is None or self.template.is_placeholder

    def _scan(self) -> None:
        self._log("→ 正在扫描 BLE 设备（6 秒）…")
        self._run_async(ble.scan_devices(6.0), self._on_scan_done)

    def _on_scan_done(self, devices) -> None:
        self.devices = devices or []
        self._log(f"✓ 发现 {len(self.devices)} 台设备", "ok")
        self.tree.delete(*self.tree.get_children())
        filt = (self.template.name_filter or "").lower() if self.template else ""
        for i, d in enumerate(self.devices):
            tags = ["stripe"] if i % 2 else []
            text = d["name"]
            if filt and filt in d["name"].lower():
                text = f"★ {text}"
            self.tree.insert("", "end", iid=d["address"],
                             values=(text, d["address"],
                                     f"{d['rssi']} dBm" if d["rssi"] is not None else "—"),
                             tags=tuple(tags))
        if not self.devices:
            self._log("（无结果：确认棒子已上电并在蓝牙范围内；本机蓝牙适配器已开启）", "warn")

    def _selected_address(self) -> Optional[str]:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先扫描并选择设备。", parent=self)
            return None
        return sel[0]

    def _enumerate(self) -> None:
        addr = self._selected_address()
        if not addr:
            return
        name = self.tree.set(addr, "name").lstrip("★ ")
        self.v_addr.set(f"{name} [{addr}]")
        self._log(f"→ 连接 {addr} 枚举 GATT…")
        self._run_async(ble.enumerate_gatt(addr, self._log), lambda lines: None)

    def _preview(self) -> None:
        if self.template is None:
            return
        try:
            payload = ble.build_payload(self.template, {"ssid": self.v_ssid.get(),
                                                        "psk": self.v_psk.get()})
        except ValueError as exc:
            messagebox.showwarning("缺少字段", str(exc), parent=self)
            return
        self._log(f"载荷预览: {payload!r}")

    def _send(self) -> None:
        if self.template is None or self._template_placeholder():
            messagebox.showwarning("协议未定稿",
                                   "模板 UUID 为全零占位符。\n"
                                   "RustSimHub 固件端 BLE 协议（GATT UUID/会话）定稿后，"
                                   "编辑 config/ble-provisioning-template.json 再试。", parent=self)
            return
        addr = self.v_addr.get().strip()
        if not addr:
            messagebox.showinfo("提示", "请先扫描并双击目标设备。", parent=self)
            return
        real_addr = addr.split("[")[-1].rstrip("]")
        try:
            payload = ble.build_payload(self.template, {"ssid": self.v_ssid.get(),
                                                        "psk": self.v_psk.get()})
        except ValueError as exc:
            messagebox.showwarning("缺少字段", str(exc), parent=self)
            return
        if not messagebox.askyesno(
                "确认发送", f"向 {addr}\n写入 {len(payload)} 字节配网载荷？", parent=self):
            return
        tmpl = self.template
        self._run_async(ble.send_payload(real_addr, tmpl, payload, self._log),
                        lambda r: None)

    # ==================================================================
    # 队列与日志
    # ==================================================================
    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ok":
                    result, on_done = payload
                    on_done(result)
                elif kind == "err":
                    msg, _ = payload
                    self._log(f"✗ {msg}", "err")
                self._set_busy(False)
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _log(self, line: str, tag: str = "") -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line.rstrip("\n") + "\n", tag or ())
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
