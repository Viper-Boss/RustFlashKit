# SPDX-License-Identifier: GPL-3.0-only
"""本地数据位置。备份、日志、设备信息只放在程序目录下的 .local/（不进 git）。"""
import os
import pathlib
import sys


def app_dir():
    if getattr(sys, 'frozen', False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent.parent


def local_dir():
    path = pathlib.Path(os.environ.get('USBTOOL_LOCAL_DIR') or app_dir() / '.local')
    path.mkdir(parents=True, exist_ok=True)
    return path


def backups_dir():
    return local_dir() / 'backups'


def dryruns_dir():
    return local_dir() / 'dryruns'


def child_env():
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    if not getattr(sys, 'frozen', False):
        env['PYTHONPATH'] = str(app_dir()) + os.pathsep + env.get('PYTHONPATH', '')
    return env
