# SPDX-License-Identifier: GPL-3.0-only
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace

import pytest

from usbtool import gui_flash

USB = [{'mode': 'usb', 'port': '1-4', 'description': 'WinUSB'}]
COM = [{'mode': 'serial', 'port': 'COM8', 'description': 'QDLoader'}]


@pytest.fixture(scope='module')
def shared_root():
    try:
        root = tk.Tk()  # 整个模块共用一个窗口：同一进程反复创建 Tk 偶尔会失败
    except tk.TclError:
        pytest.skip('没有图形环境')
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def tab(monkeypatch, tmp_path, shared_root):
    root = shared_root
    jobs = []
    app = SimpleNamespace(root=root, proc=None, loader=tk.StringVar(value='loader.mbn'),
                          banner=SimpleNamespace(say=lambda *a: None), launch=lambda job, timeout: jobs.append(job))
    monkeypatch.setattr(gui_flash.messagebox, 'showinfo', lambda *a, **k: None)
    monkeypatch.setattr(gui_flash.flashpack, 'packs_dir', lambda: tmp_path)
    notebook = ttk.Notebook(root)
    flash = gui_flash.FlashTab(notebook, app)
    flash.pack_dir, flash.manifest = tmp_path, {'name': 'n', 'version': 'v'}
    flash.jobs = jobs
    yield flash
    flash.phase_index = None
    notebook.destroy()


def devices(monkeypatch, value):
    monkeypatch.setattr(gui_flash.detect, 'detect', lambda: (value, []))


def test_two_phases_require_replug_between(tab, monkeypatch):
    devices(monkeypatch, USB)
    tab.begin_phase(0, first=True)
    assert [j['phase'] for j in tab.jobs] == ['prepare'] and tab.jobs[0]['chunk'] == 512 and tab.jobs[0]['confirmed']
    tab.on_result({'result': {'written': ['gpt'], 'skipped': [], 'done': False}})
    tab.on_exit()
    assert tab.phase_index == 1 and len(tab.jobs) == 1  # 棒子没拔：不开始下一阶段
    tab.poll()
    assert len(tab.jobs) == 1
    devices(monkeypatch, [])
    tab.poll()
    devices(monkeypatch, USB)
    tab.poll()
    assert [j['phase'] for j in tab.jobs] == ['prepare', 'images'] and tab.jobs[1]['chunk'] == 16384
    tab.on_result({'result': {'written': ['aboot'], 'skipped': ['boot'], 'done': True}})
    tab.on_exit()
    assert tab.phase_index is None and len(tab.jobs) == 2


def test_wrong_driver_or_two_devices_do_not_start(tab, monkeypatch):
    devices(monkeypatch, COM)
    tab.begin_phase(0, first=True)
    assert not tab.jobs and 'WinUSB' in tab.state.get()
    devices(monkeypatch, USB + USB)
    tab.poll()
    assert not tab.jobs and '多个' in tab.state.get()


def test_waits_while_other_operation_runs(tab, monkeypatch):
    devices(monkeypatch, USB)
    tab.app.proc = object()
    tab.begin_phase(0, first=True)
    assert not tab.jobs
    tab.app.proc = None
    tab.poll()
    assert len(tab.jobs) == 1


def test_worker_dying_without_result_stops_flow(tab, monkeypatch):
    devices(monkeypatch, USB)
    tab.begin_phase(0, first=True)
    tab.on_exit()
    assert tab.phase_index is None and '结果不明' in tab.steps.get('1.0', 'end')


def test_error_stops_and_exit_after_error_does_nothing(tab, monkeypatch):
    devices(monkeypatch, USB)
    tab.begin_phase(0, first=True)
    tab.on_error('预检未通过')
    tab.on_exit()
    assert tab.phase_index is None and len(tab.jobs) == 1 and '预检未通过' in tab.steps.get('1.0', 'end')
