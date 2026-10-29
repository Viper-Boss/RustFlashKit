# SPDX-License-Identifier: GPL-3.0-only
"""EDL 工作子进程。界面把任务写成 JSON 文件交给它；它用 '@@USBTOOL ' 前缀的 JSON 行回报事件。

放在子进程里是为了：edl 的输出/全局状态不污染界面；超时或取消时可以直接结束进程。
"""
import json
import os
import subprocess
import sys
import threading
import time
import traceback

from . import backup, paths, policy

PREFIX = '@@USBTOOL '
ACTIONS = ('probe', 'backup', 'write')
# 看门狗时限（秒）：连接阶段为整体上限；读取阶段为“多久没有任何进展”的上限。
CONNECT_LIMIT = 90
STALL_LIMIT = 120
WATCHDOG_EXIT = 4
MISSED_HELLO_HINT = '若是刚连接：设备进入 9008 后只发一次 Sahara Hello，请拔掉 USB 重新进入 9008 后立即操作。'
WATCHDOG = None


class Watchdog:
    """阶段超时后回报错误并直接结束进程（edl 的阻塞读无法从外部打断）。不重试、不重启设备。"""

    def __init__(self, on_expire=None, interval=1.0):
        self.lock = threading.Lock()
        self.phase, self.limit, self.last = None, None, time.monotonic()
        self.on_expire = on_expire or self.expire
        self.interval = interval
        self.stopped = threading.Event()
        threading.Thread(target=self.loop, daemon=True).start()

    def arm(self, phase, limit):
        with self.lock:
            self.phase, self.limit, self.last = phase, limit, time.monotonic()

    def kick(self):
        with self.lock:
            self.last = time.monotonic()

    def stop(self):
        self.stopped.set()

    def loop(self):
        while not self.stopped.wait(self.interval):
            with self.lock:
                expired = self.limit is not None and time.monotonic() - self.last > self.limit
                phase, limit = self.phase, self.limit
            if expired:
                self.on_expire(phase, limit)
                return

    @staticmethod
    def expire(phase, limit):
        hint = MISSED_HELLO_HINT if phase == 'connect' else '读取停滞，设备可能已断开或卡住。'
        emit('error', message=f'{phase} 阶段超过 {limit} 秒没有进展，已停止。{hint}未写入设备，未自动重试。')
        os._exit(WATCHDOG_EXIT)


def emit(kind, **data):
    sys.stdout.write(PREFIX + json.dumps({'type': kind, **data}, ensure_ascii=False) + '\n')
    sys.stdout.flush()
    if WATCHDOG is not None and kind in ('progress', 'log'):
        WATCHDOG.kick()


class Tee:
    """把输出同时写到原输出和日志文件，排查时不依赖界面或控制台。"""

    def __init__(self, primary, log):
        self.primary, self.log = primary, log

    def write(self, data):
        self.log.write(data)
        self.log.flush()
        if self.primary is not None:
            self.primary.write(data)
        return len(data)

    def flush(self):
        self.log.flush()
        if self.primary is not None:
            self.primary.flush()

    def isatty(self):
        return False


def validate_job(job):
    if job.get('action') not in ACTIONS:
        raise ValueError('未知任务类型')
    if job.get('mode') not in ('serial', 'usb'):
        raise ValueError('未知连接方式')
    if job['action'] == 'backup':
        names = job.get('partitions')
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ValueError('备份任务需要分区列表')
    if job['action'] == 'write':
        if job['mode'] != 'usb':
            raise ValueError('写入只支持 USB（WinUSB）方式')
        if job.get('reset_after'):
            raise ValueError('写入任务不允许自动重启设备')
        if not all(isinstance(job.get(k), str) and job.get(k) for k in ('plan', 'step', 'confirm')):
            raise ValueError('写入任务需要计划文件、步骤名和确认摘要')


def run(job, link_factory=None):
    validate_job(job)
    from . import edl_link
    digest, label = edl_link.check_loader(job['loader'], job.get('allow_unknown_loader', False))
    emit('log', message=f'加载器：{label}（sha256 {digest[:16]}…）')
    if link_factory is None:
        link_factory = edl_link.EdlLink
    if WATCHDOG is not None:
        WATCHDOG.arm('connect', CONNECT_LIMIT)
    link = link_factory(job['loader'], job['mode'], job.get('port', ''), log=lambda m: emit('log', message=m))
    if WATCHDOG is not None:
        WATCHDOG.arm('read', STALL_LIMIT)
    try:
        device = link.device_info()
        device['loader_sha256'] = digest
        emit('device', device=device)

        def progress(label, done, total):
            emit('progress', label=label, done=done, total=total)

        if job['action'] == 'probe':
            layout, _p, _b = backup.read_layout(link.read, link.set_disk_sectors)
            emit('result', action='probe', device=device, layout=layout, warnings=policy.ufi003_warnings(layout))
        elif job['action'] == 'write':
            from . import __version__, write_runner, writeplan
            plan = writeplan.load_plan(job['plan'])
            ep_out, ep_in = link.endpoints()
            report, folder = write_runner.run_step(plan, job['step'], job['confirm'], link.read, ep_out, ep_in,
                                                   log=lambda m: emit('log', message=m), progress=progress,
                                                   on_disk_sectors=link.set_disk_sectors, tool_version=__version__)
            emit('result', action='write', folder=str(folder), status=report['status'], passed=report['passed'])
        else:
            folder = backup.new_backup_dir(job.get('backup_base') or paths.backups_dir())
            emit('log', message=f'备份目录：{folder}')
            manifest = backup.run_backup(link.read, folder, job['partitions'], reread=job.get('reread', True), vote=job.get('vote', False), keep_reads=job.get('keep_reads', False),
                                         progress=progress, device=device, on_disk_sectors=link.set_disk_sectors,
                                         log=lambda m: emit('log', message=m))
            emit('result', action='backup', folder=str(folder), manifest=manifest)
        if job.get('reset_after'):
            emit('log', message='请求设备正常重启（仅一次）')
            link.reset()
            emit('log', message='设备已确认重启请求；是否回到正常系统需另行核对')
    finally:
        link.close()


def log_path(job_path):
    folder = paths.local_dir() / 'logs'
    folder.mkdir(parents=True, exist_ok=True)
    return folder / (os.path.splitext(os.path.basename(job_path))[0] + '.log')


def main(job_path, watchdog=True):
    global WATCHDOG
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    log = open(log_path(job_path), 'a', encoding='utf-8')
    old = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = Tee(old[0], log), Tee(old[1], log)
    emit('log', message=f'日志文件：{log.name}')
    if watchdog:
        WATCHDOG = Watchdog()
    try:
        with open(job_path, encoding='utf-8') as stream:
            job = json.load(stream)
        run(job)
        return 0
    except Exception as err:  # 所有失败都回报给界面，不自动重试、不自动重启设备
        emit('error', message=str(err), detail=traceback.format_exc())
        return 1
    finally:
        if WATCHDOG is not None:
            WATCHDOG.stop()
            WATCHDOG = None
        sys.stdout, sys.stderr = old
        log.close()


def worker_command(job_path):
    if getattr(sys, 'frozen', False):
        return [sys.executable, 'worker', str(job_path)]
    return [sys.executable, '-m', 'usbtool', 'worker', str(job_path)]


def start(job):
    """界面侧：写任务文件并启动子进程，返回 Popen。"""
    jobs = paths.local_dir() / 'jobs'
    jobs.mkdir(parents=True, exist_ok=True)
    job_path = jobs / f'job-{time.strftime("%Y%m%d-%H%M%S")}-{job["action"]}.json'
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    return subprocess.Popen(worker_command(job_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, cwd=str(paths.local_dir()), creationflags=flags,
                            encoding='utf-8', errors='replace', env=paths.child_env())


def parse_line(line):
    """把子进程的一行输出解析成事件；普通 edl 输出返回 log 事件。"""
    line = line.rstrip('\r\n')
    if line.startswith(PREFIX):
        try:
            return json.loads(line[len(PREFIX):])
        except ValueError:
            pass
    return {'type': 'log', 'message': line}
