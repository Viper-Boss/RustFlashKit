# SPDX-License-Identifier: GPL-3.0-only
"""顶部标题栏：应用名、说明和一个状态标签（就绪/进行中/完成/出错）。"""
import tkinter as tk

from . import theme

HEIGHT = 76
STATES = {
    'idle': ('就绪', '#9ca3af', '插上设备后开始'),
    'busy': ('进行中', theme.WARN, '正在读取，请不要拔线'),
    'happy': ('完成', '#10b981', '操作已完成'),
    'worried': ('出错', '#ef4444', '操作失败，未自动重试，请查看日志'),
    'wifi': ('Wi-Fi', '#818cf8', '试连接 45 秒内不确认会自动恢复原网络'),
}


class Banner(tk.Canvas):
    def __init__(self, parent, family):
        super().__init__(parent, height=HEIGHT, highlightthickness=0, background=theme.HEADER)
        self.family = family
        self.state = 'idle'
        self.message = STATES['idle'][2]
        self.bind('<Configure>', lambda _e: self.redraw())

    def say(self, state, message=None):
        self.state = state if state in STATES else 'idle'
        self.message = message or STATES[self.state][2]
        self.redraw()

    def redraw(self):
        self.delete('all')
        w = max(self.winfo_width(), 600)
        # 标志：圆角方块 + 信号弧
        self.round_rect(20, 18, 60, 58, 10, fill=theme.ACCENT, outline='')
        for r in (8, 14):
            self.create_arc(40 - r, 45 - r, 40 + r, 45 + r, start=45, extent=90, style='arc', outline='white', width=2)
        self.create_oval(37, 42, 43, 48, fill='white', outline='')
        self.create_text(74, 28, anchor='w', text='RustSimHub USB 工具', fill='white', font=(self.family, 14, 'bold'))
        self.create_text(75, 52, anchor='w', text='UFI003 · 9008 只读备份 · 刷写演练 · Wi-Fi 重连',
                         fill='#a5abc3', font=(self.family, 9))
        label, color, _ = STATES[self.state]
        message = self.create_text(w - 24, 54, anchor='e', text=self.message, fill='#c9cee0', font=(self.family, 9))
        pill = self.create_text(w - 38, 26, anchor='e', text=label, fill='white', font=(self.family, 9, 'bold'))
        px1, py1, px2, py2 = self.bbox(pill)
        self.round_rect(px1 - 24, py1 - 4, px2 + 14, py2 + 4, 11, fill='#2d3250', outline='#3b4166')
        middle = (py1 + py2) / 2
        self.create_oval(px1 - 15, middle - 4, px1 - 7, middle + 4, fill=color, outline='')
        self.tag_raise(pill)
        if self.bbox(message)[0] < 440:  # 窄窗口时隐藏说明，避免与标题重叠
            self.delete(message)

    def round_rect(self, x1, y1, x2, y2, r, **kw):
        points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
                  x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.create_polygon(points, smooth=True, **kw)
