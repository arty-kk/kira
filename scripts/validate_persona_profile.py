#!/usr/bin/env python
import json
import sys
from typing import Any

ALLOWED_GENDERS = {"male", "female"}
ALLOWED_SOCIALITY = {"introvert", "ambivert", "extrovert"}
ALLOWED_ZODIAC = {
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo", "Libra",
    "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
}


def _err(msg: str) -> None:
    print(f"error: {msg}")


def _is_str(value: Any, *, max_len: int | None = None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if max_len is not None and len(value.strip()) > max_len:
        return False
    return True


def _validate_temperament(temp: Any) -> tuple[bool, str | None]:
    if not isinstance(temp, dict):
        return False, "temperament must be an object"
    keys = {"sanguine", "choleric", "phlegmatic", "melancholic"}
    if set(temp.keys()) != keys:
        return False, "temperament must include sanguine/choleric/phlegmatic/melancholic"
    total = 0.0
    for k in keys:
        v = temp.get(k)
        if not isinstance(v, (int, float)):
            return False, f"temperament.{k} must be a number"
        if v < 0 or v > 1:
            return False, f"temperament.{k} must be in [0, 1]"
        total += float(v)
    if not (0.95 <= total <= 1.05):
        return False, "temperament values must sum to 1.0"
    return True, None


def validate_profile(profile: dict) -> list[str]:
    errors: list[str] = []

    if not _is_str(profile.get("id"), max_len=64):
        errors.append("id is required (string up to 64 chars)")
    if not _is_str(profile.get("name"), max_len=64):
        errors.append("name is required (string up to 64 chars)")

    age = profile.get("age")
    if not isinstance(age, int) or not (1 <= age <= 120):
        errors.append("age must be an integer between 1 and 120")

    gender = profile.get("gender")
    if gender not in ALLOWED_GENDERS:
        errors.append("gender must be 'male' or 'female'")

    zodiac = profile.get("zodiac")
    if zodiac not in ALLOWED_ZODIAC:
        errors.append("zodiac must be a valid zodiac sign")

    ok, temp_err = _validate_temperament(profile.get("temperament"))
    if not ok and temp_err:
        errors.append(temp_err)

    sociality = profile.get("sociality")
    if sociality not in ALLOWED_SOCIALITY:
        errors.append("sociality must be introvert/ambivert/extrovert")

    archetypes = profile.get("archetypes")
    if not isinstance(archetypes, list) or not archetypes:
        errors.append("archetypes must be a non-empty list")
    else:
        for item in archetypes:
            if not _is_str(item, max_len=32):
                errors.append("archetypes must be strings up to 32 chars")
                break

    if not _is_str(profile.get("role"), max_len=1000):
        errors.append("role is required (string up to 1000 chars)")

    return errors


def main() -> int:
    if len(sys.argv) != 2:
        _err("usage: python scripts/validate_persona_profile.py <profile.json>")
        return 2

    path = sys.argv[1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        _err(f"failed to read profile: {exc}")
        return 2

    if not isinstance(data, dict):
        _err("profile must be a JSON object")
        return 2

    errors = validate_profile(data)
    if errors:
        for e in errors:
            _err(e)
        return 1

    print("profile ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
