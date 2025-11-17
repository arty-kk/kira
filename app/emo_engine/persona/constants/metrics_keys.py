#app/emo_engine/persona/constants/metrics_keys.py
from functools import lru_cache
from .emotions import ALL_METRICS
from .tone_map import TONE_MAP

@lru_cache(maxsize=1)
def _all_mod_keys():

    base = list(ALL_METRICS)
    base_mods = [f"{k}_mod" if not k.endswith("_mod") else k for k in base]
    tone_mods = list(TONE_MAP.keys())
    all_mods = base_mods + tone_mods
    metrics_keys = list(dict.fromkeys(all_mods))
    return metrics_keys

metrics_keys = _all_mod_keys()