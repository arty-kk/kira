#app/emo_engine/persona/executor.py
from __future__ import annotations

import atexit
import logging
import math
import os

from typing import Optional
from concurrent.futures import Executor, ThreadPoolExecutor

logger = logging.getLogger(__name__)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def _safe_int_env(
    name: str,
    default: Optional[int] = None,
    *,
    lo: int = 1,
    hi: Optional[int] = None,
) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        v = int(str(raw).strip())
    except Exception:
        return default
    if hi is not None:
        v = min(v, hi)
    v = max(lo, v)
    return v


def _parse_cpuset_list(spec: str) -> int:
    if not spec:
        return 0
    cpus: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a_str, b_str = part.split("-", 1)
            try:
                a_i, b_i = int(a_str), int(b_str)
            except Exception:
                continue
            lo_i, hi_i = (a_i, b_i) if a_i <= b_i else (b_i, a_i)
            cpus.update(range(lo_i, hi_i + 1))
        else:
            if part.isdigit():
                cpus.add(int(part))
    return len(cpus)


def _cgroups_v2_quota_cores() -> Optional[float]:
    txt = _read_text("/sys/fs/cgroup/cpu.max")
    if not txt:
        return None
    parts = txt.split()
    if len(parts) < 2:
        return None
    quota, period = parts[0], parts[1]
    if quota == "max":
        return None
    try:
        q = int(quota)
        p = int(period)
        if q > 0 and p > 0:
            return q / p
    except Exception:
        return None
    return None


def _cgroups_v1_quota_cores() -> Optional[float]:
    qtxt = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    ptxt = _read_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if not qtxt or not ptxt:
        return None
    try:
        q = int(qtxt)
        p = int(ptxt)
        if q > 0 and p > 0:
            return q / p
    except Exception:
        return None
    return None


def _cpuset_effective_count() -> Optional[int]:
    candidates = [
        "/sys/fs/cgroup/cpuset.cpus.effective",
        "/sys/fs/cgroup/cpuset/cpuset.cpus.effective",
        "/sys/fs/cgroup/cpuset.cpus",
    ]
    for path in candidates:
        txt = _read_text(path)
        if txt:
            n = _parse_cpuset_list(txt)
            if n > 0:
                return n
    return None


def _affinity_count() -> Optional[int]:
    if hasattr(os, "sched_getaffinity"):
        try:
            return len(os.sched_getaffinity(0))
        except Exception:
            return None
    return None


def _proc_status_allowed_list() -> Optional[int]:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("Cpus_allowed_list:"):
                    spec = line.split(":", 1)[1].strip()
                    n = _parse_cpuset_list(spec)
                    return n if n > 0 else None
    except Exception:
        return None
    return None


def _os_cpu_count() -> int:
    try:
        c = os.cpu_count()
        return c if c and c > 0 else 1
    except Exception:
        return 1


def _effective_cpu() -> int:
    candidates: list[float] = []

    v2 = _cgroups_v2_quota_cores()
    if v2:
        candidates.append(v2)
        logger.debug("executor: cgroups v2 quota cores=%.3f", v2)

    v1 = _cgroups_v1_quota_cores()
    if v1:
        candidates.append(v1)
        logger.debug("executor: cgroups v1 quota cores=%.3f", v1)

    cpuset = _cpuset_effective_count()
    if cpuset:
        candidates.append(float(cpuset))
        logger.debug("executor: cpuset effective cores=%d", cpuset)

    aff = _affinity_count()
    if aff:
        candidates.append(float(aff))
        logger.debug("executor: sched_getaffinity cores=%d", aff)

    plist = _proc_status_allowed_list()
    if plist:
        candidates.append(float(plist))
        logger.debug("executor: /proc/self/status allowed_list cores=%d", plist)

    host = float(_os_cpu_count())
    candidates.append(host)
    logger.debug("executor: host cpu_count=%d", int(host))

    pos = [x for x in candidates if x and x > 0]
    logger.debug("executor: candidates=%s", [f"{x:.3f}" for x in candidates if x])
    if not pos:
        return 1
    eff = min(pos)
    return max(1, int(math.ceil(eff)))


EFFECTIVE_CPU: int = _effective_cpu()
DEFAULT_CAP: int = max(1, min(4, EFFECTIVE_CPU))
MAX_WORKERS: int = (
    _safe_int_env("GLOBAL_EXECUTOR_MAX_WORKERS", default=DEFAULT_CAP, lo=1, hi=EFFECTIVE_CPU)
    or DEFAULT_CAP
)

_IMPL = os.getenv("EXECUTOR_IMPL", "thread").strip().lower()
_EXECUTOR: Optional[Executor] = None

if _IMPL == "process":
    try:
        from concurrent.futures import ProcessPoolExecutor
        try:
            from multiprocessing import get_context
            _mp_ctx = get_context("spawn")
        except Exception:
            _mp_ctx = None

        _EXECUTOR = ProcessPoolExecutor(
            max_workers=MAX_WORKERS,
            mp_context=_mp_ctx if _mp_ctx is not None else None
        )
        logger.info(
            "executor: using ProcessPoolExecutor with %d workers (effective_cpu=%d)",
            MAX_WORKERS,
            EFFECTIVE_CPU,
        )
    except Exception:
        logger.exception(
            "executor: failed to init ProcessPoolExecutor, falling back to ThreadPoolExecutor"
        )
        _EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="persona-exec")
        _IMPL = "thread"
else:
    _EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="persona-exec")
    logger.info(
        "executor/thread: max_workers=%d (effective_cpu=%d)",
        MAX_WORKERS,
        EFFECTIVE_CPU,
    )

assert _EXECUTOR is not None
EXECUTOR: Executor = _EXECUTOR


def _shutdown_executor() -> None:
    ex = EXECUTOR
    if not ex:
        return
    try:
        ex.shutdown(wait=True, cancel_futures=True)
    except TypeError:
        ex.shutdown(wait=True)

atexit.register(_shutdown_executor)

__all__ = [
    "EXECUTOR",
    "EFFECTIVE_CPU",
    "DEFAULT_CAP",
    "MAX_WORKERS",
]