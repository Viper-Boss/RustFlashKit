# SPDX-License-Identifier: GPL-3.0-only
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['worker'] and len(argv) == 2:
        from . import worker
        return worker.main(argv[1])
    if argv:
        # The frozen Windows console defaults to the local ANSI code page when
        # its output is piped or logged. Use UTF-8 to preserve Chinese messages.
        if sys.platform == 'win32':
            for stream in (sys.stdout, sys.stderr):
                if hasattr(stream, 'reconfigure') and not stream.isatty():
                    stream.reconfigure(encoding='utf-8')
        from . import cli
        return cli.main(argv)
    from . import gui
    return gui.main()


if __name__ == '__main__':
    sys.exit(main())
