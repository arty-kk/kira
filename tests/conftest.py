import os


os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABCdef1234567890")
os.environ.setdefault("TELEGRAM_BOT_USERNAME", "testbot")
os.environ.setdefault("TELEGRAM_BOT_ID", "1")
os.environ.setdefault("WEBHOOK_URL", "https://example.invalid")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("REDIS_URL_QUEUE", "redis://localhost:6379/1")
os.environ.setdefault("REDIS_URL_VECTOR", "redis://localhost:6379/2")
os.environ.setdefault("CELERY_BROKER_URL", "redis://localhost:6379/3")
os.environ.setdefault("TWITTER_API_KEY", "test")
os.environ.setdefault("TWITTER_API_SECRET", "test")
os.environ.setdefault("TWITTER_ACCESS_TOKEN", "test")
os.environ.setdefault("TWITTER_ACCESS_TOKEN_SECRET", "test")
os.environ.setdefault("TWITTER_BEARER_TOKEN", "test")
