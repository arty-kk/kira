#app/emo_engine/persona/constants/user_prefs.py
from __future__ import annotations

from typing import Mapping

ZODIAC: list[str] = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra",
    "Scorpio","Sagittarius","Capricorn","Aquarius","Pisces",
]
ZODIAC_SET = set(ZODIAC)

SOCIALITY_KEYS = ("introvert","ambivert","extrovert")
SOCIALITY_SET = set(SOCIALITY_KEYS)

ARCHETYPES: list[str] = [
    "Nomad","Architect","Mirror","Spark","Ghost","Anchor","Muse","Trickster",
    "Hero","Sage","Explorer","Creator","Caregiver","Rebel","Lover","Jester",
]
ARCHETYPES_SET = set(ARCHETYPES)

TEMP_KEYS = ("sanguine","choleric","phlegmatic","melancholic")

TEMP_PRESETS: dict[str, dict[str, float]] = {
    "sanguine":    {"sanguine":0.45,"choleric":0.18,"phlegmatic":0.25,"melancholic":0.12},
    "choleric":    {"sanguine":0.25,"choleric":0.45,"phlegmatic":0.18,"melancholic":0.12},
    "phlegmatic":  {"sanguine":0.25,"choleric":0.12,"phlegmatic":0.45,"melancholic":0.18},
    "melancholic": {"sanguine":0.12,"choleric":0.25,"phlegmatic":0.18,"melancholic":0.45},
}

MAX_ARCH = 3

def _norm_temp_map(t: Mapping[str, float] | None) -> dict[str, float] | None:

    if not isinstance(t, dict):
        return None
    vals = {k: float(t.get(k, 0.0)) for k in TEMP_KEYS}
    vals = {k: max(0.0, min(1.0, v)) for k, v in vals.items()}
    s = sum(vals.values())
    if s <= 0.0:
        return None
    return {k: (vals[k] / s) for k in TEMP_KEYS}

def normalize_prefs(prefs: dict | None) -> dict:
    if not isinstance(prefs, dict):
        return {}
    out: dict[str, object] = {}

    name = prefs.get("name")
    if isinstance(name, str):
        name = name.strip()
        if name:
            out["name"] = name[:64]

    age = prefs.get("age")
    try:
        age_int = int(age)
        if 1 <= age_int <= 120:
            out["age"] = age_int
    except (TypeError, ValueError):
        pass

    gender = prefs.get("gender")
    if isinstance(gender, str) and gender in ("male", "female"):
        out["gender"] = gender

    z = prefs.get("zodiac")
    if isinstance(z, str) and z in ZODIAC_SET:
        out["zodiac"] = z

    tm = _norm_temp_map(prefs.get("temperament"))
    if tm:
        out["temperament"] = tm

    s = prefs.get("sociality")
    if isinstance(s, str) and s in SOCIALITY_SET:
        out["sociality"] = s

    arch = prefs.get("archetypes")
    if isinstance(arch, list):
        norm, seen = [], set()
        for a in arch:
            if not isinstance(a, str):
                continue
            if a in ARCHETYPES_SET and a not in seen:
                seen.add(a)
                norm.append(a)
            if len(norm) >= MAX_ARCH:
                break
        if norm:
            out["archetypes"] = norm

    role = prefs.get("role")
    if isinstance(role, str):
        role = role.strip()
        if role:
            out["role"] = role[:1000]

    return out

def merge_prefs(current: dict | None, new: dict | None) -> dict:
    res = dict(current or {})
    if isinstance(new, dict):
        for k in ("name","age","gender","zodiac","temperament","sociality","archetypes","role"):
            if k in new:
                res[k] = new[k]
    return res