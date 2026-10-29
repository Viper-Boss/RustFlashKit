# SPDX-License-Identifier: GPL-3.0-only
"""tkinter 图形界面。设备操作都交给工作子进程，界面只显示事件。"""
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__, backup, detect, dryrun, paths, policy, worker
from .edl_link import loader_digest

PROBE_TIMEOUT = 300  # 秒；读分区表应在几十秒内完成


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.proc = None
        self.device = None
        self.layout = None
        root.title(f'RustSimHub-UsbTool {__version__}（只读 + 演练，GPLv3）')
        root.geometry('900x680')
        root.minsize(760, 560)

        top = ttk.LabelFrame(root, text='连接')
        top.pack(fill='x', padx=8, pady=6)
        self.loader = tk.StringVar()
        ttk.Label(top, text='Firehose 加载器：').grid(row=0, column=0, sticky='w', padx=4, pady=3)
        ttk.Entry(top, textvariable=self.loader).grid(row=0, column=1, sticky='ew', padx=4)
        ttk.Button(top, text='选择…', command=self.pick_loader).grid(row=0, column=2, padx=4)
        self.loader_label = ttk.Label(top, text='未选择（需要你自己提供 prog_emmc_firehose_8916.mbn）')
        self.loader_label.grid(row=1, column=1, sticky='w', padx=4)
        ttk.Button(top, text='检测 9008 设备', command=self.detect).grid(row=2, column=0, padx=4, pady=3, sticky='w')
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(top, textvariable=self.device_var, state='readonly')
        self.device_box.grid(row=2, column=1, sticky='ew', padx=4)
        self.reset_after = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text='完成后让设备正常重启', variable=self.reset_after).grid(row=2, column=2, padx=4)
        top.columnconfigure(1, weight=1)
        self.devices = []

        tabs = ttk.Notebook(root)
        tabs.pack(fill='both', expand=True, padx=8)
        self.build_backup_tab(tabs)
        self.build_dryrun_tab(tabs)
        self.build_later_tab(tabs)

        bottom = ttk.Frame(root)
        bottom.pack(fill='both', padx=8, pady=6)
        self.progress = ttk.Progressbar(bottom, maximum=1000)
        self.progress.pack(fill='x')
        self.status = ttk.Label(bottom, text='就绪。本版本不包含任何写入设备的功能。')
        self.status.pack(fill='x')
        self.log = tk.Text(bottom, height=10, wrap='word')
        self.log.pack(fill='both', expand=True)
        self.cancel_button = ttk.Button(bottom, text='取消当前操作', command=self.cancel, state='disabled')
        self.cancel_button.pack(anchor='e', pady=3)
        root.after(100, self.pump)

    # ---------- 界面构造 ----------
    def build_backup_tab(self, tabs):
        frame = ttk.Frame(tabs)
        tabs.add(frame, text='分区表与备份（只读）')
        bar = ttk.Frame(frame)
        bar.pack(fill='x', pady=4)
        self.probe_button = ttk.Button(bar, text='读取分区表', command=self.probe)
        self.probe_button.pack(side='left', padx=4)
        self.full_disk = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text='同时全盘备份', variable=self.full_disk).pack(side='left', padx=4)
        self.reread = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text='二次回读校验（耗时加倍，推荐）', variable=self.reread).pack(side='left', padx=4)
        self.backup_button = ttk.Button(bar, text='备份所选分区', command=self.start_backup, state='disabled')
        self.backup_button.pack(side='left', padx=4)
        ttk.Button(bar, text='复核已有备份…', command=self.verify_existing).pack(side='left', padx=4)
        ttk.Button(bar, text='打开备份目录', command=lambda: self.open_folder(paths.backups_dir())).pack(side='right', padx=4)
        columns = ('index', 'name', 'first', 'size', 'note')
        self.tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode='extended')
        for col, text, width in zip(columns, ('序号', '名称', '起始扇区', '大小', '说明'), (50, 130, 110, 120, 380)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor='w')
        self.tree.pack(fill='both', expand=True, padx=4, pady=4)
        ttk.Label(frame, text='按住 Ctrl 或 Shift 可多选。备份只读设备，结果保存在程序目录 .local\\backups\\。').pack(anchor='w', padx=4)

    def build_dryrun_tab(self, tabs):
        frame = ttk.Frame(tabs)
        tabs.add(frame, text='刷写演练（不写入）')
        ttk.Label(frame, text='演练只核对：目标分区是否允许、镜像大小/格式/摘要、是否已有同一设备的已校验备份。不会连接或写入设备。',
                  wraplength=840).pack(anchor='w', padx=4, pady=4)
        row = ttk.Frame(frame)
        row.pack(fill='x', padx=4)
        self.backup_dir = tk.StringVar()
        ttk.Label(row, text='写前备份目录：').pack(side='left')
        ttk.Entry(row, textvariable=self.backup_dir).pack(side='left', fill='x', expand=True)
        ttk.Button(row, text='选择…', command=self.pick_backup_dir).pack(side='left', padx=4)
        self.write_rows = []
        for target in sorted(policy.ALLOWED_TARGETS):
            r = ttk.Frame(frame)
            r.pack(fill='x', padx=4, pady=2)
            enabled = tk.BooleanVar(value=False)
            image = tk.StringVar()
            digest = tk.StringVar()
            ttk.Checkbutton(r, text=f'{target:<9}', variable=enabled, width=11).pack(side='left')
            ttk.Entry(r, textvariable=image).pack(side='left', fill='x', expand=True)
            ttk.Button(r, text='镜像…', command=lambda v=image, e=enabled: self.pick_image(v, e)).pack(side='left', padx=2)
            ttk.Label(r, text='期望 SHA256（可选）').pack(side='left')
            ttk.Entry(r, textvariable=digest, width=24).pack(side='left', padx=2)
            self.write_rows.append((target, enabled, image, digest))
        ttk.Label(frame, text=f'禁写（硬编码，不可更改）：{", ".join(sorted(policy.DENIED_NAMES))}',
                  wraplength=840, foreground='#a00').pack(anchor='w', padx=4, pady=4)
        ttk.Button(frame, text='运行演练', command=self.run_dryrun).pack(anchor='w', padx=4)
        self.dry_text = tk.Text(frame, height=10, wrap='word')
        self.dry_text.pack(fill='both', expand=True, padx=4, pady=4)

    def build_later_tab(self, tabs):
        frame = ttk.Frame(tabs)
        tabs.add(frame, text='Wi-Fi / 短信通话导出')
        ttk.Label(frame, wraplength=840, justify='left', text=(
            '尚未实现。\n\n计划：通过 USB 网络连接 RustSimHub 设备的 Edge HTTPS 接口，以经主人批准配对的客户端身份'
            '调用已有接口重新连接 Wi-Fi（扫描选择或手动输入，带 45 秒自动回滚；不做 Wi-Fi 配置导入导出）；'
            '短信和通话记录通过已授权的查询接口导出。\n'
            '不会绕过设备鉴权，也不会直接改写设备数据库。')).pack(anchor='w', padx=8, pady=8)

    # ---------- 动作 ----------
    def append_log(self, text):
        self.log.insert('end', text + '\n')
        self.log.see('end')

    def pick_loader(self):
        path = filedialog.askopenfilename(title='选择 Firehose 加载器', filetypes=[('加载器', '*.mbn *.elf *.bin'), ('所有文件', '*.*')])
        if not path:
            return
        self.loader.set(path)
        digest = loader_digest(path)
        self.loader_label.config(text=policy.KNOWN_LOADERS.get(digest, f'未知加载器 sha256 {digest[:16]}…（使用前会再次确认）'))

    def detect(self):
        self.devices, notes = detect.detect()
        names = [f'{d["mode"]}  {d["port"]}  {d["description"]}' for d in self.devices]
        self.device_box['values'] = names
        if names:
            self.device_box.current(0)
        else:
            self.device_var.set('')
        self.append_log(f'找到 {len(self.devices)} 个 9008 设备' + ('' if names else '（确认已按键进入 9008 并安装驱动）'))
        for n in notes:
            self.append_log('提示：' + n)

    def chosen_device(self):
        index = self.device_box.current()
        if index < 0 or index >= len(self.devices):
            messagebox.showwarning('没有设备', '先点“检测 9008 设备”并选择一个设备')
            return None
        return self.devices[index]

    def base_job(self, action):
        if self.proc is not None:
            messagebox.showinfo('请稍候', '已有操作在进行')
            return None
        loader = self.loader.get()
        if not loader or not os.path.isfile(loader):
            messagebox.showwarning('缺少加载器', '先选择 Firehose 加载器文件')
            return None
        device = self.chosen_device()
        if device is None:
            return None
        allow_unknown = False
        if loader_digest(loader) not in policy.KNOWN_LOADERS:
            if not messagebox.askyesno('未知加载器', '这个加载器不在已知名单中，它会在设备内存里运行。\n确认来源可信并继续吗？'):
                return None
            allow_unknown = True
        return {'action': action, 'loader': loader, 'allow_unknown_loader': allow_unknown, 'mode': device['mode'],
                'port': device['port'], 'reset_after': self.reset_after.get()}

    def probe(self):
        job = self.base_job('probe')
        if job:
            self.launch(job, timeout=PROBE_TIMEOUT)

    def start_backup(self):
        names = [self.tree.set(i, 'name') for i in self.tree.selection()]
        if self.full_disk.get():
            names.append(backup.FULL_DISK)
        if not names:
            messagebox.showinfo('未选择', '在列表里选择要备份的分区，或勾选全盘备份')
            return
        job = self.base_job('backup')
        if not job:
            return
        job.update(partitions=names, reread=self.reread.get(), backup_base=str(paths.backups_dir()))
        self.launch(job, timeout=None)

    def launch(self, job, timeout):
        self.append_log(f'开始：{job["action"]}')
        self.proc = worker.start(job)
        self.cancel_button.config(state='normal')
        self.probe_button.config(state='disabled')
        self.backup_button.config(state='disabled')
        proc = self.proc
        threading.Thread(target=self.reader, args=(proc,), daemon=True).start()
        if timeout:
            self.root.after(timeout * 1000, lambda: self.timeout(proc))

    def reader(self, proc):
        for line in proc.stdout:
            self.events.put(worker.parse_line(line))
        proc.wait()
        self.events.put({'type': 'exit', 'code': proc.returncode, 'proc': proc})

    def timeout(self, proc):
        if self.proc is proc and proc.poll() is None:
            self.append_log('操作超时，已结束工作进程。设备可能仍停在 9008/Firehose，请检查后再操作，不要盲目重试。')
            proc.kill()

    def cancel(self):
        if self.proc is not None and self.proc.poll() is None:
            if messagebox.askyesno('取消', '结束当前操作？已读的部分保留为 .partial 文件，不会被当成有效备份。'):
                self.proc.kill()

    def pump(self):
        try:
            while True:
                self.handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def handle(self, event):
        kind = event.get('type')
        if kind == 'log':
            if event['message'].strip():
                self.append_log(event['message'])
        elif kind == 'progress':
            self.progress['value'] = event['done'] * 1000 // max(event['total'], 1)
            self.status.config(text=f'{event["label"]}：{event["done"] * 100 // max(event["total"], 1)}%')
        elif kind == 'device':
            self.device = event['device']
            chip = {True: 'MSM8916 ✓', False: '非 MSM8916', None: '未读到（设备可能已在 Firehose 模式）'}[self.device.get('chip_ok')]
            self.append_log(f'芯片：{chip}  HWID {self.device.get("hwid")}')
        elif kind == 'result' and event['action'] == 'probe':
            self.show_layout(event['layout'], event['warnings'])
        elif kind == 'result':
            self.append_log(f'备份完成并校验通过：{event["folder"]}')
            self.backup_dir.set(event['folder'])
            messagebox.showinfo('备份完成', f'备份已保存并校验：\n{event["folder"]}')
        elif kind == 'error':
            self.append_log('错误：' + event['message'])
            messagebox.showerror('操作失败', event['message'] + '\n\n没有自动重试，也没有自动重启设备。')
        elif kind == 'exit' and event['proc'] is self.proc:
            self.proc = None
            self.cancel_button.config(state='disabled')
            self.probe_button.config(state='normal')
            self.backup_button.config(state='normal' if self.layout else 'disabled')
            self.status.config(text=f'工作进程结束（退出码 {event["code"]}）')

    def show_layout(self, layout, warnings):
        self.layout = layout
        self.tree.delete(*self.tree.get_children())
        for p in layout['partitions']:
            if policy.is_denied(p['name']):
                note = '基带/NV/引导链：禁写（可备份）'
            elif p['name'] in policy.ALLOWED_TARGETS:
                note = '系统分区：可作为刷写目标'
            else:
                note = ''
            self.tree.insert('', 'end', values=(p['index'], p['name'], p['first_sector'], human(p['bytes']), note))
        self.append_log(f'分区表：主备 GPT 一致、CRC 正确，共 {len(layout["partitions"])} 个分区，磁盘 {human(layout["disk_bytes"])}')
        for w in warnings:
            self.append_log('提示：' + w)

    def verify_existing(self):
        folder = filedialog.askdirectory(title='选择备份目录', initialdir=str(paths.backups_dir()))
        if not folder:
            return
        manifest, errors = backup.verify_backup(folder)
        if manifest and not errors:
            messagebox.showinfo('复核通过', f'{len(manifest["items"])} 个镜像长度和 SHA256 与清单一致')
        else:
            messagebox.showerror('复核未通过', '\n'.join(errors))

    def pick_backup_dir(self):
        folder = filedialog.askdirectory(title='选择写前备份目录', initialdir=str(paths.backups_dir()))
        if folder:
            self.backup_dir.set(folder)

    def pick_image(self, var, enabled):
        path = filedialog.askopenfilename(title='选择镜像')
        if path:
            var.set(path)
            enabled.set(True)

    def run_dryrun(self):
        writes = [{'partition': t, 'image': i.get(), 'sha256': d.get()} for t, e, i, d in self.write_rows if e.get()]
        if not self.backup_dir.get():
            messagebox.showwarning('缺少备份', '先选择写前备份目录')
            return
        report = dryrun.run_dryrun(self.backup_dir.get(), writes)
        path = dryrun.save_report(report, paths.dryruns_dir())
        self.dry_text.delete('1.0', 'end')
        self.dry_text.insert('end', dryrun.format_report(report) + f'\n\n报告已保存：{path}\n本版本没有真实写入功能。')

    @staticmethod
    def open_folder(path):
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))  # noqa: 仅 Windows


def human(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if n < 1024 or unit == 'GiB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0
