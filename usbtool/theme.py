# SPDX-License-Identifier: GPL-3.0-only
"""简洁的浅色主题：浅灰底、白色内容区、靛蓝主色。危险提示用红色。"""
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

BG = '#f4f6fa'
CARD = '#ffffff'
BORDER = '#dfe4ec'
ACCENT = '#4f46e5'
ACCENT_HOVER = '#4338ca'
ACCENT_SOFT = '#eef0ff'
HEADER = '#1e2235'
TEXT = '#1f2937'
MUTED = '#6b7280'
DANGER = '#dc2626'
OK = '#059669'
WARN = '#d97706'


def pick_font(root):
    families = set(tkfont.families(root))
    for name in ('Microsoft YaHei UI', 'Microsoft YaHei', 'PingFang SC', 'Noto Sans CJK SC', 'Segoe UI'):
        if name in families:
            return name
    return 'TkDefaultFont'


def apply(root):
    family = pick_font(root)
    base = (family, 10)
    root.configure(background=BG)
    root.option_add('*Font', base)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', background=BG, foreground=TEXT, font=base, bordercolor=BORDER,
                    lightcolor=BG, darkcolor=BG, troughcolor=BORDER, focuscolor=ACCENT)
    style.configure('TFrame', background=BG)
    style.configure('Card.TFrame', background=CARD)
    style.configure('TLabel', background=BG, foreground=TEXT)
    style.configure('Muted.TLabel', foreground=MUTED)
    style.configure('Danger.TLabel', foreground=DANGER)
    style.configure('TLabelframe', background=BG, bordercolor=BORDER, relief='solid', borderwidth=1)
    style.configure('TLabelframe.Label', background=BG, foreground=TEXT, font=(family, 10, 'bold'))

    style.configure('TButton', background=CARD, foreground=TEXT, bordercolor=BORDER,
                    lightcolor=CARD, darkcolor=CARD, padding=(12, 5), relief='flat', borderwidth=1)
    style.map('TButton', background=[('disabled', BG), ('pressed', '#e5e7eb'), ('active', '#f3f4f6')],
              foreground=[('disabled', '#a3aab5')], bordercolor=[('active', '#c7cdd8')])
    style.configure('Accent.TButton', background=ACCENT, foreground='white', bordercolor=ACCENT,
                    lightcolor=ACCENT, darkcolor=ACCENT, font=(family, 10, 'bold'))
    style.map('Accent.TButton', background=[('disabled', '#c7c9f3'), ('pressed', ACCENT_HOVER), ('active', ACCENT_HOVER)],
              bordercolor=[('disabled', '#c7c9f3'), ('active', ACCENT_HOVER)], foreground=[('disabled', 'white')])

    style.configure('TCheckbutton', background=BG, foreground=TEXT, indicatorbackground=CARD,
                    indicatorforeground=ACCENT, indicatormargin=(0, 0, 6, 0))
    style.map('TCheckbutton', background=[('active', BG)], indicatorbackground=[('selected', ACCENT_SOFT)])
    style.configure('TEntry', fieldbackground=CARD, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    insertcolor=ACCENT, padding=5)
    style.map('TEntry', bordercolor=[('focus', ACCENT)], lightcolor=[('focus', ACCENT)])
    style.configure('TCombobox', fieldbackground=CARD, background=CARD, bordercolor=BORDER, arrowcolor=MUTED, padding=4)
    style.map('TCombobox', fieldbackground=[('readonly', CARD)], foreground=[('readonly', TEXT)],
              selectbackground=[('readonly', CARD)], selectforeground=[('readonly', TEXT)],
              bordercolor=[('focus', ACCENT)])
    root.option_add('*TCombobox*Listbox.background', CARD)
    root.option_add('*TCombobox*Listbox.selectBackground', ACCENT)

    style.configure('TNotebook', background=BG, borderwidth=0, tabmargins=(0, 8, 0, 0))
    style.configure('TNotebook.Tab', background=BG, foreground=MUTED, padding=(18, 8), bordercolor=BG,
                    lightcolor=BG, font=(family, 10))
    style.map('TNotebook.Tab', background=[('selected', CARD), ('active', '#eceff5')],
              foreground=[('selected', ACCENT)], bordercolor=[('selected', BORDER)],
              font=[('selected', (family, 10, 'bold'))])

    style.configure('Treeview', background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=28, bordercolor=BORDER)
    style.configure('Treeview.Heading', background='#f8fafc', foreground=MUTED, font=(family, 9, 'bold'),
                    relief='flat', bordercolor=BORDER)
    style.map('Treeview', background=[('selected', ACCENT_SOFT)], foreground=[('selected', TEXT)])
    style.configure('Accent.Horizontal.TProgressbar', troughcolor='#e5e7eb', background=ACCENT,
                    bordercolor='#e5e7eb', lightcolor=ACCENT, darkcolor=ACCENT, thickness=6)
    style.configure('Vertical.TScrollbar', background=BG, troughcolor=BG, arrowcolor=MUTED, bordercolor=BG)
    return family


def text_widget(parent, **options):
    """统一样式的 Text（日志/报告）。"""
    widget = tk.Text(parent, background=CARD, foreground=TEXT, insertbackground=ACCENT, relief='flat',
                     highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                     selectbackground=ACCENT_SOFT, padx=10, pady=8, **options)
    widget.tag_configure('error', foreground=DANGER)
    widget.tag_configure('ok', foreground=OK)
    widget.tag_configure('time', foreground='#9ca3af')
    return widget


def listbox(parent, **options):
    return tk.Listbox(parent, background=CARD, foreground=TEXT, relief='flat', highlightthickness=1,
                      highlightbackground=BORDER, highlightcolor=ACCENT, selectbackground=ACCENT_SOFT,
                      selectforeground=TEXT, activestyle='none', **options)
