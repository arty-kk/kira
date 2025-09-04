#app/emo_engine/persona/executor.py
import os
import atexit

from concurrent.futures import ThreadPoolExecutor

CPU = 5

MAX_WORKERS = int(os.getenv("GLOBAL_EXECUTOR_MAX_WORKERS", str(CPU * 2)))
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)

atexit.register(lambda: EXECUTOR.shutdown(wait=True, cancel_futures=True))
