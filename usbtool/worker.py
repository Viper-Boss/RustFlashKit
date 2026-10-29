# SPDX-License-Identifier: GPL-3.0-only
"""EDL 工作子进程。界面把任务写成 JSON 文件交给它；它用 '@@USBTOOL ' 前缀的 JSON 行回报事件。

放在子进程里是为了：edl 的输出/全局状态不污染界面；超时或取消时可以直接结束进程。
"""
import json
import subprocess
import sys
import time
import traceback

from . import backup, paths, policy

PREFIX = '@@USBTOOL '
ACTIONS = ('probe', 'backup')


def emit(kind, **data):
    sys.stdout.write(PREFIX + json.dumps({'type': kind, **data}, ensure_ascii=False) + '\n')
    sys.stdout.flush()


def validate_job(job):
    if job.get('action') not in ACTIONS:
        raise ValueError('未知任务类型')
    if job.get('mode') not in ('serial', 'usb'):
        raise ValueError('未知连接方式')
    if job['action'] == 'backup':
        names = job.get('partitions')
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            raise ValueError('备份任务需要分区列表')


def run(job, link_factory=None):
    validate_job(job)
    from . import edl_link
    digest, label = edl_link.check_loader(job['loader'], job.get('allow_unknown_loader', False))
    emit('log', message=f'加载器：{label}（sha256 {digest[:16]}…）')
    if link_factory is None:
        link_factory = edl_link.EdlLink
    link = link_factory(job['loader'], job['mode'], job.get('port', ''), log=lambda m: emit('log', message=m))
    try:
        device = link.device_info()
        device['loader_sha256'] = digest
        emit('device', device=device)

        def progress(label, done, total):
            emit('progress', label=label, done=done, total=total)

        if job['action'] == 'probe':
            layout, _p, _b = backup.read_layout(link.read, link.set_disk_sectors)
            emit('result', action='probe', device=device, layout=layout, warnings=policy.ufi003_warnings(layout))
        else:
            folder = backup.new_backup_dir(job.get('backup_base') or paths.backups_dir())
            emit('log', message=f'备份目录：{folder}')
            manifest = backup.run_backup(link.read, folder, job['partitions'], reread=job.get('reread', True),
                                         progress=progress, device=device, on_disk_sectors=link.set_disk_sectors,
                                         log=lambda m: emit('log', message=m))
            emit('result', action='backup', folder=str(folder), manifest=manifest)
        if job.get('reset_after'):
            emit('log', message='请求设备正常重启（仅一次）')
            link.reset()
            emit('log', message='设备已确认重启请求；是否回到正常系统需另行核对')
    finally:
        link.close()


def main(job_path):
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    try:
        with open(job_path, encoding='utf-8') as stream:
            job = json.load(stream)
        run(job)
        return 0
    except Exception as err:  # 所有失败都回报给界面，不自动重试、不自动重启设备
        emit('error', message=str(err), detail=traceback.format_exc())
        return 1


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
