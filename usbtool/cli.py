# SPDX-License-Identifier: GPL-3.0-only
"""命令行入口（与界面功能相同，便于脚本和排错）。"""
import argparse
import json
import os
import sys
import threading
import time

from . import backup, board_id, detect, dryrun, edl_link, emmc_life, flashpack, oneclick, paths, worker


PROBE_TIMEOUT = 300  # 秒；与界面一致。工作进程自身另有看门狗


def _run_job(job, timeout=None):
    proc = worker.start(job)
    code = 1
    timer = None
    if timeout:
        def expire():
            print(f'\n错误：超过 {timeout} 秒未完成，已结束工作进程（未写入设备，未自动重试）', file=sys.stderr, flush=True)
            proc.kill()
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
    try:
        return _pump(proc, code)
    finally:
        if timer:
            timer.cancel()


def _pump(proc, code):
    for line in proc.stdout:
        event = worker.parse_line(line)
        kind = event.get('type')
        if kind == 'log':
            print(event['message'])
        elif kind == 'progress':
            print(f'\r{event["label"]} {event["done"] * 100 // max(event["total"], 1)}%', end='', flush=True)
        elif kind == 'device':
            print('设备：' + json.dumps(event['device'], ensure_ascii=False))
        elif kind == 'result':
            code = 0
            if event['action'] == 'probe':
                print('棒子型号：' + board_id.describe(event['board']))
                for p in event['layout']['partitions']:
                    print(f'{p["index"]:>3} {p["name"]:<12} 起始 {p["first_sector"]:>10} 大小 {p["bytes"]:>12}')
                for w in event['warnings']:
                    print('提示：' + w)
            elif event['action'] == 'flash':
                result = event['result']
                print(f'\n阶段 {result["phase"]} 完成：写入 {result["written"]}，跳过 {result["skipped"]}')
            elif event['action'] == 'storage':
                print(emmc_life.format_report(event['report']))
                code = 0 if event['report']['available'] else 1
            elif event['action'] == 'write':
                code = 0 if event['passed'] else 1
                print(f'\n写入步骤结束：{event["status"]}（报告：{event["folder"]}；未自动重启设备）')
            else:
                print(f'\n备份完成：{event["folder"]}')
        elif kind == 'error':
            print('\n错误：' + event['message'], file=sys.stderr)
    proc.wait()
    return code if proc.returncode == 0 else 1


def _wait_for_usb_device(fresh):
    """等棒子进入 9008（WinUSB）。fresh=True 时要先看到它被拔掉。"""
    while True:
        devices, _ = detect.detect()
        if fresh:
            fresh = bool(devices)
        elif len(devices) == 1 and devices[0]['mode'] == 'usb':
            return
        elif devices and devices[0]['mode'] != 'usb':
            print('棒子的驱动不是 WinUSB，请在设备管理器换成 QHSUSB__BULK')
        time.sleep(1)


def _flash(args):
    try:
        folder = flashpack.import_pack(args.pack)
    except flashpack.PackError as err:
        print(f'错误：{err}', file=sys.stderr)
        return 1
    manifest = flashpack.check_imported(folder)
    loader = folder / manifest['loader']['file'] if manifest.get('loader') else (args.loader or edl_link.default_loader())
    if not loader:
        print('错误：刷机包没带加载器，请用 --loader 提供', file=sys.stderr)
        return 1
    print(f'刷机包：{manifest["name"]} 版本 {manifest["version"]}')
    for index, (phase, chunk) in enumerate(oneclick.PHASES):
        print('请拔掉棒子，等 5 秒，按住按键插回电脑，继续按住 3 秒再松开…')
        _wait_for_usb_device(fresh=index > 0)
        job = {'action': 'flash', 'mode': 'usb', 'port': '', 'pack_dir': str(folder), 'phase': phase, 'chunk': chunk,
               'confirmed': True, 'retry': args.retry, 'reset_after': False, 'loader': os.path.abspath(str(loader)),
               'allow_unknown_loader': bool(manifest.get('loader'))}
        if _run_job(job) != 0:
            return 1
    print('刷机完成，棒子正在重启。')
    return 0


def selfcheck():
    """确认打包后的 edl 可导入、写入方法已被禁用、libusb 可加载。不打开任何设备。"""
    from . import guard
    old = sys.argv
    sys.argv = ['edl.py', 'printgpt', '--memory=emmc']
    try:
        from edlclient.Library.firehose import firehose
    finally:
        sys.argv = old
    guard.install(firehose, guard.ReadOnlyPolicy())
    for name in guard.MUTATING_METHODS:
        try:
            getattr(firehose, name)(None)
        except guard.RefusedOperation:
            continue
        print(f'失败：{name} 没有被禁用')
        return 1
    try:
        firehose.xmlsend(None, '<?xml version="1.0" ?><data><program start_sector="0"/></data>')
        print('失败：program XML 没有被拦截')
        return 1
    except guard.RefusedOperation:
        pass
    backend = detect.libusb_backend()
    print(f'edl 已加载，{len(guard.MUTATING_METHODS)} 个写入方法已禁用，program XML 被拦截')
    print('libusb：' + ('可用' if backend is not None else '不可用（只能用 COM 口方式）'))
    print('内置加载器：' + ('有' if edl_link.default_loader() else '没有'))
    return 0


def main(argv):
    parser = argparse.ArgumentParser(prog='RustSimHub-UsbTool')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('detect', help='列出 9008 设备')
    sub.add_parser('selfcheck', help='运行环境自检（不访问设备）')
    for name in ('probe', 'backup', 'write'):
        p = sub.add_parser(name, help='按计划写入一步（只支持 USB 方式；需要 --confirm 步骤摘要）' if name == 'write' else None)
        p.add_argument('--loader', help='默认用 exe 内置的加载器')
        p.add_argument('--allow-unknown-loader', action='store_true')
        p.add_argument('--mode', choices=('serial', 'usb'))
        p.add_argument('--port', default='')
        if name != 'write':
            p.add_argument('--reset-after', action='store_true', help='完成后请求设备正常重启一次')
        if name == 'backup':
            p.add_argument('--partition', action='append', required=True, help=f'分区名，可重复；{backup.FULL_DISK} 表示全盘')
            p.add_argument('--no-reread', action='store_true')
            p.add_argument('--vote', action='store_true', help='回读不一致时再读一遍，按扇区多数表决另存未验证镜像')
            p.add_argument('--keep-reads', action='store_true', help='另存第二遍原始读数（<文件>.read2）')
        if name == 'write':
            p.add_argument('--plan', required=True)
            p.add_argument('--step', required=True, help='只执行这一步')
            p.add_argument('--confirm', required=True, help='步骤摘要（write-dryrun 会给出；至少前 16 位）')
    life = sub.add_parser('emmc-life', help='只读取 eMMC 寿命：开机状态用 --identity/--known-hosts，9008 状态用 --loader')
    life.add_argument('--usb', action='store_true', help='一键：USB 线上开机的 RustSimHub 棒子，自动找维护私钥并按棒子记住主机公钥')
    life.add_argument('--adb', action='store_true', help='原厂棒子正常开机、开着 adb 时读取')
    life.add_argument('--ssh', default='root@10.55.0.2', help='开机状态的设备地址（默认 root@10.55.0.2）')
    life.add_argument('--identity', help='维护 SSH 私钥文件')
    life.add_argument('--known-hosts', help='含设备主机公钥的 known_hosts（必须提供，不做首次自动信任）')
    life.add_argument('--loader', help='9008 状态的加载器；是否读得到取决于加载器')
    life.add_argument('--edl', action='store_true', help='9008 状态，用内置加载器')
    life.add_argument('--allow-unknown-loader', action='store_true')
    life.add_argument('--mode', choices=('serial', 'usb'))
    life.add_argument('--port', default='')
    k = sub.add_parser('pack-keygen', help='生成刷机包签名密钥（不覆盖已有文件）')
    k.add_argument('--key', required=True)
    c = sub.add_parser('pack-create', help='生成并签名刷机包')
    c.add_argument('--output', required=True)
    c.add_argument('--name', required=True)
    c.add_argument('--version', required=True)
    c.add_argument('--key', required=True)
    for image in flashpack.IMAGES:
        c.add_argument(f'--{image.lower()}', required=True, metavar='镜像文件')
    c.add_argument('--with-loader', help='把加载器打进包里（这样的包只能自用，不能公开分发）')
    im = sub.add_parser('pack-import', help='校验并导入刷机包')
    im.add_argument('pack')
    fl = sub.add_parser('flash', help='一键刷机（只支持 USB 方式）')
    fl.add_argument('--pack', required=True, help='.rshpack 文件')
    fl.add_argument('--loader', help='刷机包没带加载器时必须提供')
    fl.add_argument('--i-understand-this-erases-android', action='store_true', required=True)
    fl.add_argument('--retry', action='store_true', help='重试上次未成功的那一步')
    w = sub.add_parser('write-dryrun', help='离线核对写入计划（不打开 USB）')
    w.add_argument('--plan', required=True)
    w.add_argument('--backup-dir', required=True, help='含当前设备 gpt-primary.bin / gpt-backup.bin 的备份目录')
    w.add_argument('--step', help='只核对这一步（默认按顺序核对全部）')
    v = sub.add_parser('verify', help='离线复核备份目录')
    v.add_argument('folder')
    d = sub.add_parser('dryrun', help='刷写演练（不写入）')
    d.add_argument('--backup-dir', required=True)
    d.add_argument('--write', action='append', required=True, metavar='分区=镜像路径[@sha256]')
    args = parser.parse_args(argv)

    if args.command == 'selfcheck':
        return selfcheck()
    if args.command == 'detect':
        devices, notes = detect.detect()
        for dev in devices:
            print(f'{dev["mode"]}\t{dev["port"]}\t{dev["description"]}')
        if not devices:
            print('没有找到 9008 设备（确认已按键进入 9008，并装好高通 QDLoader 驱动或 WinUSB）')
        for n in notes:
            print('提示：' + n)
        return 0 if devices else 1
    if args.command == 'pack-keygen':
        print('公钥：' + flashpack.generate_key(args.key))
        return 0
    if args.command == 'pack-create':
        images = {image: getattr(args, image.lower()) for image in flashpack.IMAGES}
        try:
            flashpack.create_pack(args.output, args.name, args.version, images, args.key, args.with_loader)
        except (flashpack.PackError, OSError) as err:
            print(f'错误：{err}', file=sys.stderr)
            return 1
        print(f'已生成：{args.output}')
        return 0
    if args.command == 'pack-import':
        try:
            print(f'已导入：{flashpack.import_pack(args.pack)}')
        except flashpack.PackError as err:
            print(f'错误：{err}', file=sys.stderr)
            return 1
        return 0
    if args.command == 'flash':
        return _flash(args)
    if args.command == 'emmc-life' and not args.loader and not args.edl:
        from . import life_usb
        try:
            if args.usb:
                report = life_usb.read_over_usb(args.identity)
            elif args.adb:
                report = life_usb.read_over_adb()
            else:
                report = emmc_life.read_over_ssh(args.ssh, args.identity, args.known_hosts)
        except emmc_life.LifeError as err:
            print(f'错误：{err}', file=sys.stderr)
            return 1
        print(emmc_life.format_report(report))
        return 0 if report['available'] else 1
    if args.command == 'write-dryrun':
        from . import write_runner, writeplan
        try:
            plan = writeplan.load_plan(args.plan)
        except (writeplan.PlanError, OSError, ValueError) as err:
            print(f'错误：{err}', file=sys.stderr)
            return 1
        print(f'计划摘要：{plan["_sha256"]}（不打开 USB，不写入）')
        reports = write_runner.dry_run_plan(plan, args.backup_dir, args.step)
        for r in reports:
            if r['passed']:
                print(f'[通过] {r["step"]} → {r["target"]}  步骤摘要 {r["step_digest"]}')
                for s in r['segments']:
                    print(f'        扇区 {s["first_sector"]} 起 {s["sectors"]} 个  {s["payload"]}  sha256 {s["sha256"][:16]}…')
            else:
                print(f'[拒绝] {r["step"]}：' + '；'.join(r['errors']))
        return 0 if reports and all(r['passed'] for r in reports) else 1
    if args.command in ('probe', 'backup', 'write', 'emmc-life'):
        mode, port = args.mode, args.port
        if mode is None:
            devices, _ = detect.detect()
            if len(devices) != 1:
                print(f'需要恰好一个 9008 设备，现在找到 {len(devices)} 个', file=sys.stderr)
                return 1
            mode, port = devices[0]['mode'], devices[0]['port']
        loader = args.loader or edl_link.default_loader()
        if not loader:
            print('错误：这个版本没有内置加载器，请用 --loader 提供', file=sys.stderr)
            return 1
        job = {'action': 'storage' if args.command == 'emmc-life' else args.command, 'loader': os.path.abspath(loader), 'allow_unknown_loader': args.allow_unknown_loader,
               'mode': mode, 'port': port, 'reset_after': getattr(args, 'reset_after', False)}
        if args.command == 'backup':
            job.update(partitions=args.partition, reread=not args.no_reread, vote=args.vote, keep_reads=args.keep_reads,
                       backup_base=str(paths.backups_dir()))
        if args.command == 'write':
            job.update(plan=os.path.abspath(args.plan), step=args.step, confirm=args.confirm)
        return _run_job(job, PROBE_TIMEOUT if args.command in ('probe', 'emmc-life') else None)
    if args.command == 'verify':
        manifest, errors = backup.verify_backup(args.folder)
        for e in errors:
            print('错误：' + e)
        print('复核通过' if manifest and not errors else '复核未通过')
        return 0 if manifest and not errors else 1
    writes = []
    for spec in args.write:
        target, _, rest = spec.partition('=')
        image, _, digest = rest.partition('@')
        writes.append({'partition': target, 'image': image, 'sha256': digest})
    report = dryrun.run_dryrun(args.backup_dir, writes)
    path = dryrun.save_report(report, paths.dryruns_dir())
    print(dryrun.format_report(report))
    print(f'报告：{path}')
    return 0 if report['passed'] else 1
