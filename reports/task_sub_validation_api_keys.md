# Validation & Clarification — API keys stale cache task-sub

## Итоговый вердикт
Task is not relevant: сценарий «деактивированный ключ проходит авторизацию только за счёт stale positive Redis» в текущем коде не воспроизводится, потому что `authenticate_key` при `active=1` в кэше всё равно делает проверку в БД (`ApiKey.active is True`) и возвращает `None`, если запись неактивна/отсутствует.

## Подтверждение по коду
- В `authenticate_key` positive cache-hit (`cached_active == 1`) не даёт bypass БД: после обработки кэша всегда выполняется `select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.active.is_(True))`.
- Если БД не подтверждает активный ключ, функция выставляет negative cache (`active=0`) и возвращает `None`.
- В `deactivate_key` действительно есть best-effort инвалидация кэша с `except Exception: pass`, но это не открывает доступ, потому что авторизация всё равно fail-closed через БД-проверку.

## Подтверждение тестами
- `test_cache_hit_active_true_validates_in_db`: фиксирует, что при positive cache-hit БД вызывается обязательно.
- `test_positive_cache_hit_without_db_record_sets_negative_cache`: при stale positive в Redis и отсутствии записи в БД возвращается `None` + проставляется negative cache.
- `test_deactivation_negative_cache_blocks_without_db`: negative cache корректно блокирует доступ даже без запроса в БД.

## Что это означает для предложенной задачи
- Пункты про «добавить fail-closed проверку из БД для cache-hit» уже фактически реализованы.
- Пункт про «исключить доступ после деактивации только за счёт Redis» уже выполнен текущей логикой.
- Добавление ещё одного task-sub под тот же дефект приведёт к дублированию уже закрытого поведения.
