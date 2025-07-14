cat >app/services/addons/__init__.py<< EOF
#app/services/addons/__init__.py
from .group_battle import start_battle_job
from .price_fetcher import price_fetcher
from .group_ping import group_ping
from .personal_ping import personal_ping
from .twitter_manager import generate_and_post_tweet

__all__ = [
    "start_battle_job",
    "price_fetcher",
    "group_ping",
    "personal_ping",
    "generate_and_post_tweet",
]
EOF