import os
import pathlib
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_alembic_upgrade_sql_works_without_redis_env_when_database_url_set():
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql+asyncpg://user:pass@localhost:5432/dbname"
    env.pop("REDIS_URL", None)
    env.pop("REDIS_URL_QUEUE", None)
    env.pop("REDIS_URL_VECTOR", None)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "-c",
            "alembic/alembic.ini",
            "upgrade",
            "head",
            "--sql",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    combined_output = f"{result.stdout}\n{result.stderr}".lower()

    assert result.returncode == 0, combined_output
    assert "redis" not in combined_output
