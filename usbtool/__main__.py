# SPDX-License-Identifier: GPL-3.0-only
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['worker'] and len(argv) == 2:
        from . import worker
        return worker.main(argv[1])
    if argv:
        from . import cli
        return cli.main(argv)
    from . import gui
    return gui.main()


if __name__ == '__main__':
    sys.exit(main())
