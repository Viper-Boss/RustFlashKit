# SPDX-License-Identifier: GPL-3.0-only
"""“Wi-Fi 重连”页：经 USB 网络连设备 Edge，用工具自己的已授权密钥登录后试连接新 Wi-Fi。"""
import hashlib
import threading
import tkinter as tk
import uuid
from tkinter import messagebox, ttk

from . import edge, edge_store

POLL_MS = 3000


class WifiTab:
    def __init__(self, tabs, app):
        self.app = app
        self.session = None
        self.trial = None
        self.pending_begin = None  # (ssid, passphrase, idempotency_key)：结果不明时用同一编号重新查询
        self.networks = []
        frame = ttk.Frame(tabs)
        tabs.add(frame, text='Wi-Fi 重连')
        ttk.Label(frame, wraplength=840, justify='left', text=(
            '用 USB 线连接棒子和电脑，电脑上的 USB 网卡需设为 10.55.0.1/29（见 README）。'
            '首次使用：连接并核对设备 → 在设备网页（主人登录）打开配对窗口 → 点“申请授权” → 在网页上批准工具。'
            '之后每次只需“登录”。试连接 45 秒内不确认会自动恢复原网络。')).pack(anchor='w', padx=6, pady=4)

        row = ttk.Frame(frame)
        row.pack(fill='x', padx=6, pady=2)
        ttk.Label(row, text='设备地址：').pack(side='left')
        saved = edge_store.load_device()
        self.address = tk.StringVar(value=saved['address'] if saved else edge.DEFAULT_ADDRESS)
        ttk.Entry(row, textvariable=self.address, width=22).pack(side='left')
        self.connect_button = ttk.Button(row, text='连接并核对设备', command=self.connect)
        self.connect_button.pack(side='left', padx=4)
        self.pair_button = ttk.Button(row, text='申请授权', command=self.pair, state='disabled')
        self.pair_button.pack(side='left', padx=4)
        self.login_button = ttk.Button(row, text='登录', command=self.login, state='disabled')
        self.login_button.pack(side='left', padx=4)
        ttk.Button(row, text='忘记此设备', command=self.forget).pack(side='right')
        self.device_label = ttk.Label(frame, text='未连接')
        self.device_label.pack(anchor='w', padx=6)

        body = ttk.Frame(frame)
        body.pack(fill='both', expand=True, padx=6, pady=4)
        left = ttk.Frame(body)
        left.pack(side='left', fill='both', expand=True)
        self.list_button = ttk.Button(left, text='读取设备已发现的网络', command=self.load_networks, state='disabled')
        self.list_button.pack(anchor='w')
        self.listbox = tk.Listbox(left, height=8)
        self.listbox.pack(fill='both', expand=True, pady=2)
        self.listbox.bind('<<ListboxSelect>>', self.pick_network)
        right = ttk.Frame(body)
        right.pack(side='left', fill='y', padx=8)
        ttk.Label(right, text='网络名称（SSID）').pack(anchor='w')
        self.ssid = tk.StringVar()
        ttk.Entry(right, textvariable=self.ssid, width=30).pack(anchor='w')
        ttk.Label(right, text='密码（WPA2 个人）').pack(anchor='w', pady=(6, 0))
        self.passphrase = tk.StringVar()
        ttk.Entry(right, textvariable=self.passphrase, width=30, show='•').pack(anchor='w')
        self.begin_button = ttk.Button(right, text='试连接', command=self.begin, state='disabled')
        self.begin_button.pack(anchor='w', pady=6)
        self.state_label = ttk.Label(right, text='没有试连接', wraplength=260)
        self.state_label.pack(anchor='w')
        buttons = ttk.Frame(right)
        buttons.pack(anchor='w', pady=4)
        self.confirm_button = ttk.Button(buttons, text='确认保存', command=lambda: self.action('wifi_confirm'), state='disabled')
        self.confirm_button.pack(side='left')
        self.cancel_button = ttk.Button(buttons, text='取消试连接', command=lambda: self.action('wifi_cancel'), state='disabled')
        self.cancel_button.pack(side='left', padx=4)
        self.query_button = ttk.Button(buttons, text='重新查询', command=self.query, state='disabled')
        self.query_button.pack(side='left')
        if saved:
            self.device_label.config(text=f'已信任设备：{saved["origin"]}  证书 {saved["cert_sha256"][:16]}…（点“连接并核对设备”复核）')

    # ---------- 通用 ----------
    def background(self, work, done, label):
        def run():
            try:
                result = work()
            except Exception as err:  # 把错误交回界面线程显示
                self.app.root.after(0, lambda: self.failed(label, err))
                return
            self.app.root.after(0, lambda: done(result))
        threading.Thread(target=run, daemon=True).start()

    def failed(self, label, err):
        self.app.append_log(f'{label}失败：{err}')
        if isinstance(err, edge.Uncertain):
            self.state_label.config(text='结果不明：请求可能已到达设备。只能“重新查询”，不会自动重做。设备 45 秒计时回退仍有效。')
            self.query_button.config(state='normal')
            return
        messagebox.showerror(f'{label}失败', str(err))

    def transport(self, device):
        return edge.Transport(device['address'], device['origin'], device['cert_sha256'])

    def logged_in(self, yes):
        state = 'normal' if yes else 'disabled'
        self.list_button.config(state=state)
        self.begin_button.config(state=state)

    # ---------- 设备核对 ----------
    def connect(self):
        address = self.address.get().strip()

        def work():
            probe = edge.Transport(address, None, None, timeout=8)
            der = probe.fetch_certificate()
            digest = hashlib.sha256(der).hexdigest()
            saved = edge_store.load_device()
            if saved and saved['cert_sha256'] == digest:
                return 'known', saved
            if saved:
                raise edge.PinMismatch('这台设备的证书和已信任的不一致。若确实换了设备或重装了系统，先点“忘记此设备”。')
            origin = edge.origin_from_certificate(der, probe.port)
            if not origin:
                raise edge.EdgeError('设备证书里没有主机名，无法确定网址')
            candidate = {'address': address, 'origin': origin, 'cert_sha256': digest}
            device_key = edge.identity(self.transport(candidate))
            return 'new', {**candidate, 'device_public_key': device_key}

        def done(result):
            kind, device = result
            if kind == 'new':
                ok = messagebox.askyesno('首次连接，请核对', (
                    f'网址：{device["origin"]}\n证书 SHA256：{device["cert_sha256"]}\n设备公钥：{device["device_public_key"]}\n\n'
                    '请确认这是你用 USB 线直连的那台棒子（可与设备网页显示的设备公钥比对）。信任它吗？'))
                if not ok:
                    return
                device = edge_store.save_device(device['address'], device['origin'], device['cert_sha256'], device['device_public_key'])
            self.session = edge.Session(self.transport(device), device['device_public_key'], edge_store.load_or_create_key())
            self.device_label.config(text=f'已连接：{device["origin"]}  证书 {device["cert_sha256"][:16]}…  设备公钥 {device["device_public_key"][:16]}…')
            self.pair_button.config(state='normal')
            self.login_button.config(state='normal')
            self.app.append_log('设备已核对')

        self.background(work, done, '连接设备')

    def forget(self):
        if messagebox.askyesno('忘记设备', '删除本机保存的设备证书指纹和设备公钥？（工具私钥保留）'):
            edge_store.forget_device()
            self.session = None
            self.logged_in(False)
            self.pair_button.config(state='disabled')
            self.login_button.config(state='disabled')
            self.device_label.config(text='未连接')

    def pair(self):
        def done(pairing_id):
            public = self.session.public.hex()
            text = (f'已提交授权申请。请在设备网页（主人登录）批准：\n\n申请编号：{pairing_id}\n工具公钥：{public}\n'
                    f'申请权限：{", ".join(edge.TOOL_SCOPES)}\n\n请核对编号和公钥一致后再批准。批准后点“登录”。')
            self.app.append_log(text.replace('\n\n', '\n'))
            messagebox.showinfo('等待主人批准', text)
        self.background(self.session.pair, done, '申请授权')

    def login(self):
        def done(_token):
            self.app.append_log('工具已登录（会话 15 分钟）')
            self.logged_in(True)
        self.background(self.session.login, done, '登录')

    # ---------- Wi-Fi ----------
    def load_networks(self):
        def done(result):
            self.networks = result['networks']
            self.listbox.delete(0, 'end')
            for n in self.networks:
                note = {'wpa2_personal': 'WPA2', 'open': '开放（不支持）', 'unsupported': '不支持'}[n['security']]
                self.listbox.insert('end', f'{n["ssid"]}  信号 {n["strength"]}%  {note}{"  · 当前连接" if n.get("connected") else ""}')
            if not result['wifi_enabled']:
                self.app.append_log('设备 Wi-Fi 当前关闭；本工具不会开启无线')
            self.app.append_log(f'读取到 {len(self.networks)} 个网络（设备缓存，没有发起新扫描）')
        self.background(self.session.wifi_networks, done, '读取网络列表')

    def pick_network(self, _event):
        selection = self.listbox.curselection()
        if selection:
            network = self.networks[selection[0]]
            if not network['selectable']:
                messagebox.showinfo('不支持', '只支持 WPA2 个人网络')
                return
            self.ssid.set(network['ssid'])
            self.passphrase.set('')

    def begin(self):
        if self.trial and self.trial['state'] not in edge.TERMINAL_STATES:
            messagebox.showinfo('请稍候', '当前试连接还没结束')
            return
        ssid, passphrase = self.ssid.get(), self.passphrase.get()
        if not edge.wifi_credentials_valid(ssid, passphrase):
            messagebox.showwarning('格式不对', '网络名需 1–32 字节；密码需 8–63 位可打印字符或 64 位十六进制')
            return
        self.pending_begin = (ssid, passphrase, str(uuid.uuid4()))
        self.passphrase.set('')
        self.begin_button.config(state='disabled')
        self.send_begin()

    def send_begin(self):
        ssid, passphrase, key = self.pending_begin
        self.background(lambda: self.session.wifi_begin(ssid, passphrase, key), self.show_trial, '试连接')

    def query(self):
        if self.trial:
            self.background(lambda: self.session.wifi_action('wifi_status', self.trial['trial_id']), self.show_trial, '查询')
        elif self.pending_begin:
            self.send_begin()  # 同一编号重发：设备返回原来那次试连接的状态，不会再开一次

    def action(self, op):
        if not self.trial:
            return
        self.confirm_button.config(state='disabled')
        self.background(lambda: self.session.wifi_action(op, self.trial['trial_id']), self.show_trial,
                        '确认保存' if op == 'wifi_confirm' else '取消试连接')

    def show_trial(self, trial):
        self.trial = trial
        names = {'starting': '正在开始', 'connecting': '正在连接', 'ready': '已连上，等待你确认保存', 'saving': '正在保存',
                 'saved': '已保存', 'rolled_back': '已恢复原连接', 'unknown': '结果不明，请检查设备；不会自动重试'}
        self.state_label.config(text=f'{names[trial["state"]]} · 剩余 {trial.get("expires_in_seconds", "?")} 秒')
        terminal = trial['state'] in edge.TERMINAL_STATES
        self.confirm_button.config(state='normal' if trial['state'] == 'ready' else 'disabled')
        self.cancel_button.config(state='disabled' if terminal else 'normal')
        self.query_button.config(state='normal')
        if terminal:
            self.pending_begin = None
            self.begin_button.config(state='normal')
            self.app.append_log(f'Wi-Fi 试连接结束：{names[trial["state"]]}')
        else:
            trial_id = trial['trial_id']
            self.app.root.after(POLL_MS, lambda: self.poll(trial_id))

    def poll(self, trial_id):
        # 只读查询：试连接未结束时定时刷新状态
        if self.trial and self.trial['trial_id'] == trial_id and self.trial['state'] not in edge.TERMINAL_STATES:
            self.background(lambda: self.session.wifi_action('wifi_status', trial_id), self.show_trial, '查询')
