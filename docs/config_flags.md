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
- `API_FALLBACK_RL_MAX_KEYS` (default: `10000`) — max number of keys retained by the in-memory fallback limiter; if unset or `<= 0`, the safe default `10000` is used (not unlimited).
- `API_IDEMPOTENCY_TTL_SEC` (default: `3600`) — TTL for Idempotency-Key cached responses.
- `API_QUEUE_MAX_PAYLOAD_BYTES` (default: `131072`) — max size for API queue payloads.
- `API_QUEUE_SNAPSHOT_SEC` (default: `60`) — interval for logging queue depth in the API worker.

## Bot queue limits
- `BOT_QUEUE_MAX_PAYLOAD_BYTES` (default: `65536`) — max size for bot queue payloads.

## TLS configuration (webhook + API)
- `USE_SELF_SIGNED_CERT` (default: `false`) — enables local TLS for both webhook and API processes.
- `CERTS_DIR` — base directory for certificate files used by defaults.
- `WEBHOOK_CERT` / `WEBHOOK_KEY` — certificate and private key paths. By default these paths are built from `CERTS_DIR`.
- `API_CERT` / `API_KEY` — optional API-specific cert/key overrides; if unset or empty, API uses `WEBHOOK_CERT` / `WEBHOOK_KEY`.

Unified behavior for webhook and API:
- when `USE_SELF_SIGNED_CERT=true`, both processes must have readable cert/key files;
- missing certificate or key file is treated as a configuration error and startup is stopped with an exception;
- when `USE_SELF_SIGNED_CERT=false`, local TLS is not started and external TLS termination (for example, reverse proxy / LB) is expected.
