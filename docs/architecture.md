# Архитектура исполнения (контуры)

## Контур API
1. **API** принимает запрос, валидирует payload, формирует `request_id` и ставит задачу в очередь.
   - Точка входа: `main.py`
   - Эндпоинт: `app/api/conversation.py`
   - Списание происходит в `conversation_endpoint` до постановки job в очередь; после получения `result` от воркера возврат выполняется для 5xx и ошибок `invalid_payload`/`voice_transcription_failed`.
2. **Queue** сохраняет job в Redis‑очереди.
   - Очередь: `app/api/conversation.py`
3. **Worker** забирает job, выполняет обработку, вызывает responder и возвращает результат.
   - Worker: `app/tasks/api_worker.py`
4. **Responder** строит контекст/память и формирует финальный ответ.
   - Responder core: `app/services/responder/core.py`
5. **Response** возвращается в API и отдается клиенту.
   - Ответ: `app/api/conversation.py`

## Контур Telegram
1. **Telegram** отправляет webhook.
   - Webhook: `app/bot/components/webhook.py`
2. **Dispatcher** маршрутизирует событие в обработчики.
   - Запуск и подключение бота: `main.py`
3. **Responder** формирует ответ на сообщение.
   - Responder core: `app/services/responder/core.py`

## Ручная проверка конкурентного списания
1. Создать пользователя с `free_requests=1` и `paid_requests=0`.
2. Отправить два параллельных запроса в `POST /api/v1/conversation` с валидным API-ключом.
3. Убедиться, что один запрос успешен, а второй возвращает `402` (недостаточно запросов).
