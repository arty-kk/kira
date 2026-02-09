# Config flags overview

This document summarizes the main behavior flags and limits. Defaults below match `app/config.py`.

## UI / menu flags
- `SHOW_SHOP_BUTTON` (default: `true`) — show the Shop button in the private menu.
- `SHOW_REQUESTS_BUTTON` (default: inherits `SHOW_SHOP_BUTTON`) — show the Requests button.
- `SHOW_CHANNEL_BUTTON` (default: `false`) — show the Channel button.
- `SHOW_PERSONA_BUTTON` (default: `true`) — show the Persona button.
- `SHOW_MEMORY_CLEAR_BUTTON` (default: `true`) — show the Clear memory button.
- `SHOW_API_BUTTON` (default: `true`) — show the API button.

## API limits / safeguards
- `API_RATELIMIT_PER_MIN` (default: `60`) — per-key rate limit.
- `API_RATELIMIT_BURST_FACTOR` (default: `2`) — burst factor for the per-key limiter.
- `API_RATELIMIT_PER_IP_PER_MIN` (default: `360`) — per-IP limit.
- `API_RATELIMIT_FALLBACK_PER_MIN` (default: `10`) — fallback per-key limit when Redis is unavailable.
- `API_RATELIMIT_FALLBACK_PER_IP_PER_MIN` (default: `30`) — fallback per-IP limit when Redis is unavailable.
- `API_IDEMPOTENCY_TTL_SEC` (default: `3600`) — TTL for Idempotency-Key cached responses.
- `API_QUEUE_MAX_PAYLOAD_BYTES` (default: `131072`) — max size for API queue payloads.
- `API_QUEUE_SNAPSHOT_SEC` (default: `60`) — interval for logging queue depth in the API worker.

## Bot queue limits
- `BOT_QUEUE_MAX_PAYLOAD_BYTES` (default: `65536`) — max size for bot queue payloads.
