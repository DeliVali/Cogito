"""Process control that POSIX gets from /proc, process groups and PR_SET_PDEATHSIG, for
Windows.

Linux code keeps its own calls; these are what the same callers use when `os.name ==
"nt"`. Process trees, liveness, start times and listening ports come from psutil (the only
dependency here, installed on Windows only). What dies with whom comes from a Job Object
with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: every process this core starts (an agent's shell,
a model server) is assigned to one job whose only handle this process holds, so when the
core exits for any reason, crash included, Windows ends them and everything they started
(children join their parent's job by default). The same mechanism Rust's bootstrap uses to
keep a build's processes from outliving it (src/bootstrap/src/utils/job.rs, read
2026-10-24).
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Sequence

IS_WINDOWS = os.name == "nt"

#: `subprocess` flags that start a child in its own process group without a console of
#: its own: the Windows counterpart of `start_new_session=True`. Zero elsewhere.
NEW_GROUP = (
    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
)


def _psutil():
    import psutil

    return psutil


def alive(pid: int) -> bool:
    """Whether a process with this PID exists and has not exited."""
    psutil = _psutil()
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return psutil.pid_exists(pid)


def start_time(pid: int) -> str:
    """When `pid` started, so a PID and its start time name one process, never a later
    one that reused the number. Empty when it cannot be read."""
    psutil = _psutil()
    try:
        return f"{psutil.Process(pid).create_time():.3f}"
    except (psutil.Error, OSError):
        return ""


def tree(root: int) -> list[int]:
    """`root` and every process below it."""
    psutil = _psutil()
    try:
        process = psutil.Process(root)
        return [root, *(child.pid for child in process.children(recursive=True))]
    except psutil.Error:
        return [root]


def children(pid: int) -> list[int]:
    psutil = _psutil()
    try:
        return [child.pid for child in psutil.Process(pid).children()]
    except psutil.Error:
        return []


def terminate_trees(roots: Sequence[int], grace: float) -> None:
    """End every process under `roots`: `terminate()` each, wait up to `grace` seconds,
    then `kill()` what is left. On Windows both are TerminateProcess; the wait only lets a
    process that was already exiting finish on its own."""
    psutil = _psutil()
    processes = []
    for root in roots:
        for pid in tree(root):
            try:
                processes.append(psutil.Process(pid))
            except psutil.Error:
                continue
    for process in processes:
        try:
            process.terminate()
        except psutil.Error:
            continue
    _, still = psutil.wait_procs(processes, timeout=grace)
    for process in still:
        try:
            process.kill()
        except psutil.Error:
            continue


def listening_ports(root: int) -> tuple[int | None, tuple[int, ...]]:
    """The first process below `root` that listens on a TCP port, and its ports."""
    psutil = _psutil()
    try:
        below = psutil.Process(root).children(recursive=True)
    except psutil.Error:
        return None, ()
    for process in below:
        try:
            connections = process.net_connections(kind="tcp")
        except psutil.Error:
            continue
        ports = sorted({c.laddr.port for c in connections if c.status == psutil.CONN_LISTEN})
        if ports:
            return process.pid, tuple(ports)
    return None, ()


# -- the job every child joins ------------------------------------------------------------

_job_lock = threading.Lock()
_job_handle: int | None = None


def _job() -> int | None:
    """This process's kill-on-close Job Object, created on first use. None when Windows
    refuses one (an environment that forbids nested jobs); children then only die with an
    explicit stop."""
    global _job_handle
    with _job_lock:
        if _job_handle is not None:
            return _job_handle
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kill_on_close = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        extended_info_class = 9  # JobObjectExtendedLimitInformation
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = kill_on_close
        if not kernel32.SetInformationJobObject(
            handle, extended_info_class, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel32.CloseHandle(handle)
            return None
        _job_handle = handle
        return handle


def bind_to_this_process(process: subprocess.Popen) -> bool:
    """Put `process` in this process's kill-on-close job, so it (and whatever it starts)
    ends when this process does. False when that could not be done."""
    if not IS_WINDOWS:
        return False
    job = _job()
    if job is None:
        return False
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    return bool(kernel32.AssignProcessToJobObject(job, int(process._handle)))  # noqa: SLF001


def wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True
