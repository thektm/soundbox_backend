"""Small PID-1 style supervisor for a shared-socket Daphne cluster."""
from __future__ import annotations

import math
import os
import signal
import socket
import subprocess
import sys
import time

HOST = os.getenv('DAPHNE_BIND', '0.0.0.0')
PORT = int(os.getenv('DAPHNE_PORT', '8000'))
BACKLOG = max(128, int(os.getenv('DAPHNE_BACKLOG', '2048')))


def _effective_cpu_count() -> int:
    """Respect host affinity and Docker/cgroup CPU quotas when available."""
    candidates = [os.cpu_count() or 1]
    try:
        candidates.append(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        pass

    # cgroup v2: '<quota> <period>' or 'max <period>'.
    try:
        quota, period = open('/sys/fs/cgroup/cpu.max', encoding='utf-8').read().strip().split()[:2]
        if quota != 'max' and int(period) > 0:
            candidates.append(max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError, IndexError):
        # cgroup v1 fallback.
        try:
            quota = int(open('/sys/fs/cgroup/cpu/cpu.cfs_quota_us', encoding='utf-8').read())
            period = int(open('/sys/fs/cgroup/cpu/cpu.cfs_period_us', encoding='utf-8').read())
            if quota > 0 and period > 0:
                candidates.append(max(1, math.ceil(quota / period)))
        except (OSError, ValueError):
            pass
    return max(1, min(candidates))


def worker_count():
    configured = int(os.getenv('DAPHNE_WORKERS', '0') or 0)
    if configured > 0:
        return max(1, min(configured, 32))
    # Roughly one protocol-server process per CPU, capped to avoid multiplying
    # PostgreSQL/Redis concurrency beyond what a single-host stack can absorb.
    return min(_effective_cpu_count(), 8)


def main():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((HOST, PORT))
    listener.listen(BACKLOG)
    listener.set_inheritable(True)
    fd = listener.fileno()
    desired = worker_count()
    if not os.getenv('ASGI_THREADS'):
        # Channels may open one DB connection per sync worker thread. Keep the
        # aggregate sync-thread budget bounded as Daphne process count grows.
        os.environ['ASGI_THREADS'] = str(max(4, min(8, 32 // max(1, desired))))
    children = []
    stopping = False

    def spawn():
        proc = subprocess.Popen(
            ['daphne', '--fd', str(fd), '--proxy-headers', '--verbosity', '1', 'soundbox.asgi:application'],
            pass_fds=(fd,),
        )
        children.append(proc)
        print(f'daphne cluster: worker pid={proc.pid} started', flush=True)

    def stop(_signum=None, _frame=None):
        nonlocal stopping
        stopping = True
        for proc in list(children):
            if proc.poll() is None:
                proc.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, stop)

    print(
        f'daphne cluster: listening {HOST}:{PORT} workers={desired} '
        f'asgi_threads={os.environ.get("ASGI_THREADS")}',
        flush=True,
    )
    for _ in range(desired):
        spawn()

    while not stopping:
        for proc in list(children):
            code = proc.poll()
            if code is None:
                continue
            children.remove(proc)
            print(f'daphne cluster: worker pid={proc.pid} exited code={code}', file=sys.stderr, flush=True)
            if not stopping:
                time.sleep(0.25)
                spawn()
        time.sleep(0.25)

    deadline = time.monotonic() + 15
    for proc in children:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
    listener.close()


if __name__ == '__main__':
    main()
