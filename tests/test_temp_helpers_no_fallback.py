from pathlib import Path


def test_workers_use_shared_temp_helpers_without_local_fallbacks() -> None:
    targets = [
        Path("app/tasks/api_worker.py"),
        Path("app/tasks/queue_worker.py"),
    ]
    forbidden_tokens = [
        "test loaders may stub package tree",
        "try:\n    from app.core.temp_files import managed_temp_file, open_binary_read",
        "async def open_binary_read(path: str):",
        "async def managed_temp_file(",
    ]

    for target in targets:
        content = target.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in content, f"{target} still contains forbidden fallback token: {token}"
