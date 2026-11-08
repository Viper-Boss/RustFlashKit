# SPDX-License-Identifier: GPL-3.0-only
"""PyInstaller 入口。"""
import sys

from usbtool.__main__ import main

if __name__ == '__main__':
    sys.exit(main())
