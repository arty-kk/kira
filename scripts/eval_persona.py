#!/usr/bin/env python
import os
import sys
from statistics import mean


def _seed_env() -> None:
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCdef1234567890")
    os.environ.setdefault("TELEGRAM_BOT_USERNAME", "testbot")
    os.environ.setdefault("TELEGRAM_BOT_ID", "1")
    os.environ.setdefault("WEBHOOK_URL", "https://example.invalid")
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
    os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
    os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
    os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")


_seed_env()

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.emo_engine.persona.memory import precision_recall_at_k

SAMPLES = [
    {
        "expected": ["name_to_call", "timezone", "coffee_pref"],
        "predicted": ["timezone", "coffee_pref", "hobby"],
    },
    {
        "expected": ["pet_name", "birthday"],
        "predicted": ["pet_name", "favorite_color"],
    },
    {
        "expected": ["work_role", "project"],
        "predicted": ["project", "work_role", "deadline"],
    },
]

BASELINE = {
    "precision": 0.56,
    "recall": 0.67,
    "f1": 0.61,
}


def _avg_metrics(samples: list[dict], k: int = 3) -> dict:
    precisions = []
    recalls = []
    f1s = []
    for sample in samples:
        metrics = precision_recall_at_k(sample["expected"], sample["predicted"], k)
        precisions.append(metrics["precision"])
        recalls.append(metrics["recall"])
        f1s.append(metrics["f1"])
    return {
        "precision": mean(precisions),
        "recall": mean(recalls),
        "f1": mean(f1s),
    }


def _compare(baseline: dict, current: dict) -> dict:
    return {
        "precision_delta": current["precision"] - baseline["precision"],
        "recall_delta": current["recall"] - baseline["recall"],
        "f1_delta": current["f1"] - baseline["f1"],
    }


def main() -> None:
    current = _avg_metrics(SAMPLES)
    delta = _compare(BASELINE, current)
    print("Persona memory metrics (precision/recall@k)")
    print("baseline:", {k: round(v, 3) for k, v in BASELINE.items()})
    print("current:", {k: round(v, 3) for k, v in current.items()})
    print("delta:", {k: round(v, 3) for k, v in delta.items()})

    drift_threshold = 0.15
    if abs(delta["f1_delta"]) > drift_threshold:
        print("warning: drift above threshold")


if __name__ == "__main__":
    main()
