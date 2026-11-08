# SPDX-License-Identifier: GPL-3.0-only
"""“一键刷机”页：导入刷机包 -> 确认一次 -> 自动预检、备份、写入；中途提示拔插一次。"""
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import detect, flashpack, oneclick, theme

POLL_MS = 800
CONFIRM_WORD = '确认刷机'
HOW_TO_ENTER = '请拔掉棒子，等 5 秒，按住按键插回电脑，继续按住 3 秒再松开（指示灯不亮）。'


class FlashTab:
    def __init__(self, tabs, app):
        self.app = app
        self.pack_dir = None
        self.manifest = None
        self.phase_index = None
        self.retry = False
        self.seen_gone = False
        self.running = False
        self.pending_next = None
        frame = ttk.Frame(tabs)
        tabs.add(frame, text='一键刷机')
        ttk.Label(frame, wraplength=880, justify='left', text=(
            '把原厂 UFI003 刷成 RustSimHub 系统。只需要导入刷机包、点开始，中途按提示拔插一次。'
            '工具会先备份并检查芯片，基带/NV/校准分区不会被写。')).pack(anchor='w', padx=6, pady=6)
        bar = ttk.Frame(frame)
        bar.pack(fill='x', padx=6, pady=4)
        self.import_button = ttk.Button(bar, text='1. 导入刷机包…', command=self.import_pack)
        self.import_button.pack(side='left')
        self.pack_label = ttk.Label(bar, text='还没有导入刷机包', style='Muted.TLabel')
        self.pack_label.pack(side='left', padx=10)
        bar2 = ttk.Frame(frame)
        bar2.pack(fill='x', padx=6, pady=4)
        self.start_button = ttk.Button(bar2, text='2. 开始刷机', command=self.start, state='disabled', style='Accent.TButton')
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(bar2, text='停止等待', command=self.stop_waiting, state='disabled')
        self.stop_button.pack(side='left', padx=6)
        self.state = tk.StringVar(value='')
        ttk.Label(frame, textvariable=self.state, wraplength=880, justify='left', style='Title.TLabel').pack(anchor='w', padx=6, pady=8)
        self.steps = theme.text_widget(frame, height=9, wrap='word')
        self.steps.pack(fill='both', expand=True, padx=6, pady=4)
        self.restore_last()

    def note(self, text):
        self.steps.insert('end', text + '\n')
        self.steps.see('end')

    def restore_last(self):
        folders = [p for p in flashpack.packs_dir().iterdir() if p.is_dir() and not p.name.endswith('.partial')]
        for folder in sorted(folders, key=lambda p: p.stat().st_mtime, reverse=True):
            manifest = flashpack.check_imported(folder)
            if manifest:
                self.use_pack(folder, manifest)
                return

    def use_pack(self, folder, manifest):
        self.pack_dir, self.manifest = folder, manifest
        self.pack_label.config(text=f'{manifest["name"]}  版本 {manifest["version"]}（签名和摘要已校验）')
        self.start_button.config(state='normal')

    def import_pack(self):
        path = filedialog.askopenfilename(title='选择刷机包', filetypes=[('RustSimHub 刷机包', '*.rshpack'), ('所有文件', '*.*')])
        if not path:
            return
        self.import_button.config(state='disabled')
        self.start_button.config(state='disabled')
        self.pack_label.config(text='正在校验并导入…')

        def progress(done, total):
            self.app.root.after(0, lambda: self.pack_label.config(text=f'正在校验并导入… {done * 100 // max(total, 1)}%'))

        def work():
            try:
                folder = flashpack.import_pack(path, progress)
                manifest = flashpack.check_imported(folder)
                self.app.root.after(0, lambda: self.use_pack(folder, manifest))
            except flashpack.PackError as err:
                message = str(err)
                self.app.root.after(0, lambda: self.import_failed(message))
            self.app.root.after(0, lambda: self.import_button.config(state='normal'))

        threading.Thread(target=work, daemon=True).start()

    def import_failed(self, message):
        self.pack_label.config(text='导入失败')
        messagebox.showerror('导入失败', message)

    def start(self):
        if self.app.proc is not None:
            messagebox.showinfo('请稍候', '已有操作在进行')
            return
        if not self.manifest.get('loader') and not self.app.loader.get():
            messagebox.showwarning('缺少加载器', '这个刷机包没有带加载器，请先在上方选择 Firehose 加载器')
            return
        answer = simpledialog.askstring('确认刷机', (
            f'将把棒子刷成“{self.manifest["name"]} {self.manifest["version"]}”。\n\n'
            '会改写：分区表、aboot、boot、NSHSTATE、rootfs，棒子原来的安卓系统和数据都会被清掉。\n'
            '不会写：基带、NV、校准分区（写入前后都会核对）。\n\n'
            f'确认的话请输入：{CONFIRM_WORD}'), parent=self.app.root)
        if answer != CONFIRM_WORD:
            return
        self.steps.delete('1.0', 'end')
        self.retry = False
        self.begin_phase(0, first=True)

    def begin_phase(self, index, first=False):
        self.phase_index = index
        self.seen_gone = first
        self.stop_button.config(state='normal')
        self.start_button.config(state='disabled')
        self.app.banner.say('busy', '等待棒子进入 9008')
        self.state.set(('第一步：' if first else '就差最后一步：') + HOW_TO_ENTER)
        self.poll()

    def stop_waiting(self):
        self.phase_index = None
        self.state.set('已停止。可以随时重新点“开始刷机”，已写好的内容会自动跳过。')
        self.stop_button.config(state='disabled')
        self.start_button.config(state='normal')

    def poll(self):
        if self.phase_index is None:
            return
        if self.app.proc is not None:  # 别的操作还在跑：等它结束，不打开设备
            self.app.root.after(POLL_MS, self.poll)
            return
        devices, _notes = detect.detect()
        if not self.seen_gone:
            # 上一阶段结束后要先看到棒子被拔掉：再次连接必须是全新进入的 9008
            self.seen_gone = not devices
        elif devices:
            usb = [d for d in devices if d['mode'] == 'usb']
            if len(devices) > 1:
                self.state.set('检测到多个 9008 设备，请只插一根棒子')
            elif usb:
                self.launch_phase()
                return
            else:
                self.state.set('棒子已进入 9008，但驱动不是 WinUSB。请在设备管理器把它换成 QHSUSB__BULK（WinUSB），换好后会自动继续。')
        self.app.root.after(POLL_MS, self.poll)

    def launch_phase(self):
        phase, chunk = oneclick.PHASES[self.phase_index]
        self.stop_button.config(state='disabled')
        names = {'prepare': '正在备份、检查芯片并写分区表，请不要拔线…', 'images': '正在写入系统，大约需要 10–20 分钟，请不要拔线…'}
        self.state.set(names[phase])
        loader = self.manifest.get('loader')
        job = {'action': 'flash', 'mode': 'usb', 'port': '', 'pack_dir': str(self.pack_dir), 'phase': phase, 'chunk': chunk,
               'confirmed': True, 'retry': self.retry, 'reset_after': False,
               'loader': str(self.pack_dir / loader['file']) if loader else self.app.loader.get(),
               'allow_unknown_loader': bool(loader)}
        self.running = True
        self.pending_next = None
        self.app.launch(job, timeout=None)

    def on_result(self, event):
        result = event['result']
        for name in result['written']:
            self.note(f'{name}：已写入并校验通过')
        for name in result['skipped']:
            self.note(f'{name}：已是目标内容，跳过')
        if result['done']:
            self.phase_index = None
            self.state.set('刷机完成，棒子正在重启。首次开机需要几分钟。')
            self.app.banner.say('happy', '刷机完成')
            self.start_button.config(state='normal')
            messagebox.showinfo('刷机完成', '全部写入并校验通过，棒子已请求重启。')
        else:
            self.pending_next = self.phase_index + 1

    def on_error(self, message):
        self.pending_next = None
        self.running = False
        was_running = self.phase_index is not None
        self.phase_index = None
        if not was_running:
            return
        self.state.set('已停止，详情见下方。已写好的内容不会丢，重新开始时会自动跳过。')
        self.note('已停止：' + message)
        self.stop_button.config(state='disabled')
        self.start_button.config(state='normal')
        if '再次确认' in message and messagebox.askyesno('重试？', message + '\n\n要重试这一步吗？（需要重新进入 9008）'):
            self.retry = True
            self.begin_phase(0)

    def on_exit(self):
        next_phase, self.pending_next = self.pending_next, None
        if self.phase_index is None or not self.running:
            return
        self.running = False
        if next_phase is not None:
            self.begin_phase(next_phase)
        else:  # 被取消或进程异常退出，没有结果也没有错误事件
            self.on_error('工作进程提前结束（可能被取消）。写入结果不明，重新开始时会核对已写内容。')
