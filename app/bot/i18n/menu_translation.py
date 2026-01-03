#app/bot/i18n/menu_translation.py
from __future__ import annotations

from typing import Dict, Tuple

Lang = str

CHANNEL_URL = "https://t.me/synchatica"

# Language picker buttons
LANG_BUTTONS: Dict[Lang, str] = {
    "en": "🇺🇸 English",
    "ru": "🇷🇺 Русский",
}

# Gender picker buttons (used in UI keyboards)
GENDER_LABELS: Dict[Lang, Tuple[str, str]] = {
    "en": ("👨 Male", "👩 Female"),
    "ru": ("👨 Мужской", "👩 Женский"),
}


# ---------------------------
# Messages (grouped + unified)
# ---------------------------

_EN: Dict[str, str] = {
    # Private / entry
    "private.choose_lang": "🔎 Choose your language",
    "private.channel": "📢 Join the channel to stay up to date with the latest updates.",
    "private.channel_url": CHANNEL_URL,
    "private.need_gift_soft": "😕 You're out of requests.\nOpen the shop to buy more 💬 or send a 🎁 gift.",
    "private.need_purchase": "⚠️ To continue, please buy chat requests.",

    # Rate limiting / blocks
    "private.pm_blocked": (
        "🚫 You’ve been rate-limited for 4 hours due to spam/abuse attempts. "
        "Messages to the bot are temporarily blocked."
    ),

    # Errors
    "errors.voice_recognition_failed": "⚠️ Voice recognition failed. Please try again.",
    "errors.voice_generic": "⚠️ Cannot process voice message: {reason}",
    "errors.image_generic": (
        "⚠️ Cannot process image: {reason}\n"
        "Please send exactly one image (≤ 5 MB) in a single message."
    ),
    "errors.doc_unsupported": "⚠️ Unsupported file. Please send an image or a JSON knowledge base file.",

    # Gender
    "gender.prompt": "<b>Please select your gender:</b>",
    "gender.male": "👨 Male",
    "gender.female": "👩 Female",

    # Menu
    "menu.main": "🏠 Main menu",
    "menu.shop": "🛒 Shop",
    "menu.requests": "🛒 Shop",
    "menu.link": "📢 Channel",
    "menu.persona": "🧬 Persona",
    "menu.token": "🪙 Token",
    "menu.api": "🔑 API",
    "menu.memory_clear": "🧹 Clear memory",

    # Common UI
    "ui.back": "◀ Back",
    "ui.close": "✖️ Close",

    # Persona flow
    "persona.start": "🧬 Tune how I behave for you. Four quick steps. You can reset anytime.",
    "persona.zodiac.title": "<b>Step 1 · Zodiac</b>\n♈ Pick a sign. It only nudges style and tone.",
    "persona.temperament.title": (
        "<b>Step 2 · Temperament</b>\n"
        "🔥 Choose a dominant classic type. It shapes pacing, intensity, and risk-taking."
    ),
    "persona.sociality.title": "<b>Step 3 · Sociality</b>\n🗣 How outgoing should I be by default?",
    "persona.archetypes.title": "<b>Step 4 · Archetypes</b>\n🎭 Pick up to 3. They bias narrative voice and priorities.",
    "persona.next": "▶️ Next",
    "persona.back": "◀ Back",
    "persona.skip": "⏭ Skip",
    "persona.done": "✅ Done",
    "persona.reset": "♻️ Reset",
    "persona.cancel": "✖️ Close",
    "persona.saved": "✅ Persona updated. Changes will apply to your next messages.",
    "persona.reset.ok": "♻️ Persona settings reset to balanced defaults.",
    "persona.cancel.ok": "❎ Cancelled.",
    "persona.expired": "⏳ Your persona session expired. Starting over.",
    "persona.invalid_archetype": "⚠️ Invalid archetype",
    "persona.pick.limit": "⚠️ You can pick up to {MAX}.",
    "persona.preview": "👀 Preview",
    "persona.preview.title": "👀 Preview",
    "persona.preview.failed": "⚠️ Failed to render preview",
    "persona.preview.zodiac": "Zodiac",
    "persona.preview.temperament": "Temperament",
    "persona.preview.sociality": "Sociality",
    "persona.preview.archetypes": "Archetypes",
    "persona.social.introvert": "Introvert",
    "persona.social.ambivert": "Ambivert",
    "persona.social.extrovert": "Extrovert",
    "persona.temp.sanguine": "Sanguine",
    "persona.temp.choleric": "Choleric",
    "persona.temp.phlegmatic": "Phlegmatic",
    "persona.temp.melancholic": "Melancholic",

    # Shop
    "shop.title": "<b>🛒 Shop</b>",
    "shop.subtitle": "Choose gifts or buy requests.",
    "shop.title.home": "<b>🛒 Shop</b>",
    "shop.subtitle.home": "Choose gifts or buy requests.",
    "shop.title.gifts": "<b>🎁 Gifts for your persona</b>",
    "shop.subtitle.gifts": "Each gift gives you extra requests. Pick something cute 😊",
    "shop.title.reqs": "<b>⚡ Buy requests</b>",
    "shop.subtitle.reqs": "Buy 💬 requests with ⭐ to keep chatting.",
    "shop.balance": "📊 Requests left: 💬 <b>{remaining}</b>",
    "shop.tab.gifts": "🎁 Gifts",
    "shop.tab.requests": "⚡️ Requests",
    "shop.tab.home": "◀ Back",
    "shop.open.gifts": "🎁 All gifts",
    "shop.open.reqs": "⚡ Buy requests",
    "shop.gift.button": "{emoji} • 💬 {req} • ⭐️ {stars}",
    "shop.gift.invoice_title": "🎁 Gift: {gift}",
    "shop.gift.invoice_desc": "💬 {req} requests for a gift • ⭐️ {stars}",
    "shop.gift.success": "🎁 Delivered: {gift} • 💬 {req} requests\n📊 Left: 💬 <b>{remaining}</b>",
    "shop.gift.success_duplicate": "✅ Payment already processed.\n📊 Left: 💬 <b>{remaining}</b>",

    # Payments
    "payments.you_have": "📊 You have 💬 <b>{remaining}</b> chat requests left.\nYou can buy more using ⭐.",
    "payments.buy_button": "💬 {req} = ⭐ {stars}",
    "payments.invoice_title": "🛒 Buy 💬 {req} chat requests",
    "payments.invoice_desc": "Get 💬 {req} chat requests for ⭐ {stars}",
    "payments.success": (
        "✅ Success! You purchased 💬 {req} chat requests.\n"
        "📊 Now you have 💬 <b>{remaining}</b> chat requests left."
    ),
    "payments.success_duplicate": "✅ Payment already processed.\n📊 Requests left: 💬 <b>{remaining}</b>",
    "payments.success_duplicate_short": "✅ Payment already processed.",
    "payments.gift_delivered": "✅ Gift delivered.",
    "payments.error": "⚠️ Payment error. Please try again.",
    "payments.invalid_details": "❌ Invalid payment details. Please contact support.",
    "payments.cancel_button": "❌ Cancel",
    "payments.too_frequent": "⏱ Too frequent. Try again in a second.",
    "payments.gen_error": "⚠️ Couldn't create the invoice. Please try again later.",
    "payments.pending_wait": "⏳ Payment pending. Pay the invoice above or cancel.",
    "payments.pending_wait_tier": "⏳ Payment pending for 💬 {req}. Pay the invoice above or cancel.",
    "payments.pending_wait_gift": (
        "⏳ Payment pending.\n"
        "🎁 Gift: {gift}\n"
        "Pay the invoice above or cancel."
    ),
    "payments.pending_exists": "⏳ Payment pending. Pay the invoice above or cancel.",
    "payments.pending_exists_tier": "⏳ Payment pending for 💬 {req}. Pay the invoice above or cancel.",
    "payments.pending_exists_gift": (
        "⏳ Payment pending.\n"
        "🎁 Gift: {gift}\n"
        "Pay the invoice above or cancel."
    ),
    "payments.pending_expired": "⏳ Invoice expired. Please try again.",
    "payments.pending.cancel_button": "❌ Cancel payment",
    "payments.cancelled": "❎ Payment cancelled.",

    # Gifts (titles)
    "gifts.bag.title": "Handbag",
    "gifts.cake.title": "Dessert",
    "gifts.coffee.title": "Coffee",
    "gifts.flower.title": "Rose",
    "gifts.music.title": "Song",
    "gifts.perfume.title": "Perfume",
    "gifts.ring.title": "Ring",
    "gifts.trip.title": "Travel",

    # API
    "api.title": "<b>Conversation API</b>",
    "api.status.active": "Status: 🟢 Active",
    "api.status.inactive": "Status: 🔴 Disabled",
    "api.usage": "Usage: {total} calls, avg {avg_latency} ms",
    "api.no_key": "You don't have an API key yet.",
    "api.base_url": "API URL: {url}",
    "api.note.backend_only": "🔒 Use this key only on your backend.",
    "api.note.docs": "📘 The full API guide is below.",
    "api.button.new": "🔑 New key",
    "api.button.rotate": "🔄 Rotate key",
    "api.button.disable": "⏸ Disable key",
    "api.button.howto": "📘 How to use?",
    "api.button.back": "◀ Back",
    "api.rotate.title": "<b>New API key</b>",
    "api.rotate.save": "💾 Save your API key. You'll be able to view it again for a limited time in the API menu.",
    "api.rotate.disabled_old": "⛔ All previous keys are now disabled.",
    "api.rotate.use": "🧩 Use it in your backend according to the documentation.",
    "api.rotate.again": "ℹ️ You can view it again in the API menu (for a limited time).",
    "api.delete.done": (
        "⏸ API key disabled.\n"
        "All requests with your key(s) are now rejected.\n"
        "You can create a new key at any time."
    ),
    "api.keys.title": "Your API keys:",
    "api.key.item": "{status} #{id} • …{suffix}",
    "api.key.not_found": "⚠️ Key not found",
    "api.key.show.title": "API key",
    "api.key.show.unavailable": "⚠️ Key value is not available.",
    "api.key.button.show": "👁 Show",
    "api.key.button.enable": "▶️ Enable",
    "api.key.button.disable": "⏸ Disable",
    "api.key.button.drop": "🗑 Delete",
    "api.key.button.kb": "📚 KB",

    # KB (Knowledge Base)
    "kb.title": "<b>📚 Knowledge base for this key</b>",
    "kb.field.id": "ID",
    "kb.field.status": "Status",
    "kb.field.version": "Version",
    "kb.field.items": "Items",
    "kb.field.chunks": "Chunks",
    "kb.status.pending": "pending",
    "kb.status.building": "building",
    "kb.status.ready": "ready",
    "kb.status.failed": "failed",
    "kb.status.none": "— no KB —",
    "kb.cleared": "✅ Knowledge base for this key has been cleared.",
    "kb.button.upload": "📤 Upload JSON",
    "kb.button.clear": "🗑 Clear KB",
    "kb.upload.no_slot": (
        "⚠️ No API key is selected.\n"
        "Open the API menu, choose a key and press the “📚 KB” button first."
    ),
    "kb.upload.download_failed": "⚠️ Failed to upload the file. Please try again.",
    "kb.upload.read_failed": "⚠️ Failed to read the file. Please try again.",
    "kb.upload.empty": "⚠️ The file is empty or contains only whitespace.",
    "kb.upload.bad_encoding": "⚠️ Invalid encoding. Please send a valid UTF-8 JSON file.",
    "kb.upload.bad_json": "⚠️ Invalid JSON. Please send a valid UTF-8 JSON file.",
    "kb.upload.expect_array": "⚠️ Expected a JSON array of objects.",
    "kb.upload.no_items": "⚠️ No valid items were found in the JSON file.",
    "kb.upload.too_large": "⚠️ The JSON file is too large for upload.",
    "kb.upload.accepted": "✅ Knowledge base file accepted.\nThe index will be rebuilt shortly.",
    "kb.upload.hint": (
        "📚 Send a JSON file with an array of objects.\n"
        "Required fields: id, text, tags.\n"
        "I'll rebuild the knowledge base for this API key."
    ),
    "kb.upload.truncated": "ℹ️ Imported {kept} of {total} items (limit).",

    # Memory
    "memory.clear.confirm": (
        "⚠️ This will erase the persona's memory of you.\n"
        "This action cannot be undone.\n"
        "Continue?"
    ),
    "memory.clear.confirm_yes": "✅ Yes",
    "memory.clear.confirm_no": "◀ Back",
    "memory.clear.done": "✅ Done. Persona memory has been cleared.",
    "memory.clear.cancelled": "❎ Cancelled.",
}

_RU: Dict[str, str] = {
    # Private / entry
    "private.choose_lang": "🔎 Выберите язык",
    "private.channel": "📢 Заходи в канал, чтобы быть в курсе последних обновлений.",
    "private.channel_url": CHANNEL_URL,
    "private.need_gift_soft": "😕 Запросы закончились.\nОткрой магазин: купи ещё 💬 или подари 🎁 подарок.",
    "private.need_purchase": "⚠️ Чтобы продолжить, купите запросы для общения.",

    # Rate limiting / blocks
    "private.pm_blocked": (
        "🚫 Вы ограничены на 4 часа из-за попыток спама/злоупотребления. "
        "Сообщения боту временно блокируются."
    ),

    # Errors
    "errors.voice_recognition_failed": "⚠️ Не удалось распознать голос. Попробуйте ещё раз.",
    "errors.voice_generic": "⚠️ Не удалось обработать голосовое сообщение: {reason}",
    "errors.image_generic": (
        "⚠️ Не удалось обработать изображение: {reason}\n"
        "Пожалуйста, отправьте ровно одно изображение (≤ 5 МБ) одним сообщением."
    ),
    "errors.doc_unsupported": "⚠️ Неподдерживаемый файл. Пришлите изображение или JSON-файл базы знаний.",

    # Gender
    "gender.prompt": "<b>Пожалуйста, выберите ваш пол:</b>",
    "gender.male": "👨 Мужской",
    "gender.female": "👩 Женский",

    # Menu
    "menu.main": "🏠 Главное меню",
    "menu.shop": "🛒 Магазин",
    "menu.requests": "🛒 Магазин",
    "menu.link": "📢 Канал",
    "menu.persona": "🧬 Персона",
    "menu.token": "🪙 Токен",
    "menu.api": "🔑 API",
    "menu.memory_clear": "🧹 Очистить память",

    # Common UI
    "ui.back": "◀ Назад",
    "ui.close": "✖️ Закрыть",

    # Persona flow
    "persona.start": "🧬 Подстрой, как я веду себя с тобой. Четыре быстрых шага. В любой момент можно сбросить.",
    "persona.zodiac.title": "<b>Шаг 1 · Зодиак</b>\n♈ Выбери знак. Это лишь слегка влияет на стиль и тон.",
    "persona.temperament.title": (
        "<b>Шаг 2 · Темперамент</b>\n"
        "🔥 Выбери доминирующий классический тип. Он задаёт темп, интенсивность и склонность к риску."
    ),
    "persona.sociality.title": "<b>Шаг 3 · Общительность</b>\n🗣 Какой уровень общительности выбрать по умолчанию?",
    "persona.archetypes.title": "<b>Шаг 4 · Архетипы</b>\n🎭 Выбери до 3. Они влияют на голос и приоритеты.",
    "persona.next": "▶️ Далее",
    "persona.back": "◀ Назад",
    "persona.skip": "⏭ Пропустить",
    "persona.done": "✅ Готово",
    "persona.reset": "♻️ Сброс",
    "persona.cancel": "✖️ Закрыть",
    "persona.saved": "✅ Персона обновлена. Изменения применятся к следующим сообщениям.",
    "persona.reset.ok": "♻️ Настройки персоны сброшены на сбалансированные значения.",
    "persona.cancel.ok": "❎ Отменено.",
    "persona.expired": "⏳ Сессия настройки персоны истекла. Начинаем заново.",
    "persona.invalid_archetype": "⚠️ Недопустимый архетип",
    "persona.pick.limit": "⚠️ Можно выбрать до {MAX}.",
    "persona.preview": "👀 Превью",
    "persona.preview.title": "👀 Превью",
    "persona.preview.failed": "⚠️ Не удалось показать превью",
    "persona.preview.zodiac": "Знак зодиака",
    "persona.preview.temperament": "Темперамент",
    "persona.preview.sociality": "Общительность",
    "persona.preview.archetypes": "Архетипы",
    "persona.social.introvert": "Интроверт",
    "persona.social.ambivert": "Амбиверт",
    "persona.social.extrovert": "Экстраверт",
    "persona.temp.sanguine": "Сангвиник",
    "persona.temp.choleric": "Холерик",
    "persona.temp.phlegmatic": "Флегматик",
    "persona.temp.melancholic": "Меланхолик",

    # Shop
    "shop.title": "<b>🛒 Магазин</b>",
    "shop.subtitle": "Выбирай подарки или покупай запросы.",
    "shop.title.home": "<b>🛒 Магазин</b>",
    "shop.subtitle.home": "Выбирай подарки или покупай запросы.",
    "shop.title.gifts": "<b>🎁 Подарки персоне</b>",
    "shop.subtitle.gifts": "Каждый подарок даёт дополнительные запросы. Выбирай, чем порадовать 😊",
    "shop.title.reqs": "<b>⚡ Купить запросы</b>",
    "shop.subtitle.reqs": "Покупай 💬 запросы за ⭐, чтобы продолжить общение.",
    "shop.balance": "📊 Осталось запросов: 💬 <b>{remaining}</b>",
    "shop.tab.gifts": "🎁 Подарки",
    "shop.tab.requests": "⚡️ Запросы",
    "shop.tab.home": "◀ Назад",
    "shop.open.gifts": "🎁 Все подарки",
    "shop.open.reqs": "⚡ Купить запросы",
    "shop.gift.button": "{emoji} • 💬 {req} • ⭐️ {stars}",
    "shop.gift.invoice_title": "🎁 Подарок: {gift}",
    "shop.gift.invoice_desc": "💬 {req} запросов за подарок • ⭐️ {stars}",
    "shop.gift.success": "🎁 Доставлено: {gift} • 💬 {req} запросов\n📊 Осталось: 💬 <b>{remaining}</b>",
    "shop.gift.success_duplicate": "✅ Платёж уже обработан.\n📊 Осталось: 💬 <b>{remaining}</b>",

    # Payments
    "payments.you_have": "📊 У вас осталось 💬 <b>{remaining}</b> запросов для общения.\nВы можете купить ещё за ⭐.",
    "payments.buy_button": "💬 {req} = ⭐ {stars}",
    "payments.invoice_title": "🛒 Купить 💬 {req} запросов",
    "payments.invoice_desc": "Получите 💬 {req} запросов за ⭐ {stars}",
    "payments.success": (
        "✅ Успех! Вы приобрели 💬 {req} запросов для общения.\n"
        "📊 Теперь у вас 💬 <b>{remaining}</b> запросов."
    ),
    "payments.success_duplicate": "✅ Платёж уже обработан.\n📊 Осталось запросов: 💬 <b>{remaining}</b>",
    "payments.success_duplicate_short": "✅ Платёж уже обработан.",
    "payments.gift_delivered": "✅ Подарок доставлен.",
    "payments.error": "⚠️ Ошибка оплаты. Попробуйте ещё раз.",
    "payments.invalid_details": "❌ Некорректные данные платежа. Напишите в поддержку.",
    "payments.cancel_button": "❌ Отмена",
    "payments.too_frequent": "⏱ Слишком часто. Попробуйте через секунду.",
    "payments.gen_error": "⚠️ Не удалось создать счёт. Попробуйте позже.",
    "payments.pending_wait": "⏳ Ожидается оплата. Счёт выше — оплатите или отмените.",
    "payments.pending_wait_tier": "⏳ Ожидается оплата за 💬 {req}. Счёт выше — оплатите или отмените.",
    "payments.pending_wait_gift": (
        "⏳ Ожидается оплата.\n"
        "🎁 Подарок: {gift}\n"
        "Счёт выше — оплатите или отмените."
    ),
    "payments.pending_exists": "⏳ Ожидается оплата. Счёт выше — оплатите или отмените.",
    "payments.pending_exists_tier": "⏳ Ожидается оплата за 💬 {req}. Счёт выше — оплатите или отмените.",
    "payments.pending_exists_gift": (
        "⏳ Ожидается оплата.\n"
        "🎁 Подарок: {gift}\n"
        "Счёт выше — оплатите или отмените."
    ),
    "payments.pending_expired": "⏳ Срок действия счёта истёк. Попробуйте ещё раз.",
    "payments.pending.cancel_button": "❌ Отменить оплату",
    "payments.cancelled": "❎ Оплата отменена.",

    # Gifts (titles)
    "gifts.bag.title": "Сумка",
    "gifts.cake.title": "Десерт",
    "gifts.coffee.title": "Кофе",
    "gifts.flower.title": "Роза",
    "gifts.music.title": "Песня",
    "gifts.perfume.title": "Духи",
    "gifts.ring.title": "Кольцо",
    "gifts.trip.title": "Путешествие",

    # API
    "api.title": "<b>Conversation API</b>",
    "api.status.active": "Статус: 🟢 Включено",
    "api.status.inactive": "Статус: 🔴 Выключено",
    "api.usage": "Использование: {total} вызовов, средняя задержка {avg_latency} мс",
    "api.no_key": "У вас ещё нет API-ключа.",
    "api.base_url": "API URL: {url}",
    "api.note.backend_only": "🔒 Используйте этот ключ только в своём backend.",
    "api.note.docs": "📘 Полное руководство по API — ниже.",
    "api.button.new": "🔑 Новый ключ",
    "api.button.rotate": "🔄 Поменять ключ",
    "api.button.disable": "⏸ Отключить ключ",
    "api.button.howto": "📘 Как использовать?",
    "api.button.back": "◀ Назад",
    "api.rotate.title": "<b>Новый API-ключ</b>",
    "api.rotate.save": "💾 Сохраните ключ API. Его можно будет посмотреть ещё раз ограниченное время в меню API.",
    "api.rotate.disabled_old": "⛔ Все предыдущие ключи отключены.",
    "api.rotate.use": "🧩 Используйте ключ в backend согласно документации.",
    "api.rotate.again": "ℹ️ Ключ можно посмотреть ещё раз в меню API (ограниченное время).",
    "api.delete.done": (
        "⏸ API-ключ отключён.\n"
        "Все запросы с вашими ключами теперь отклоняются.\n"
        "Вы можете в любой момент создать новый ключ."
    ),
    "api.keys.title": "Ваши API-ключи:",
    "api.key.item": "{status} #{id} • …{suffix}",
    "api.key.not_found": "⚠️ Ключ не найден",
    "api.key.show.title": "API-ключ",
    "api.key.show.unavailable": "⚠️ Значение ключа недоступно.",
    "api.key.button.show": "👁 Показать",
    "api.key.button.enable": "▶️ Включить",
    "api.key.button.disable": "⏸ Выключить",
    "api.key.button.drop": "🗑 Удалить",
    "api.key.button.kb": "📚 БЗ",

    # KB (Knowledge Base)
    "kb.title": "<b>📚 База знаний для этого ключа</b>",
    "kb.field.id": "ID",
    "kb.field.status": "Статус",
    "kb.field.version": "Версия",
    "kb.field.items": "Элементов",
    "kb.field.chunks": "Чанков",
    "kb.status.pending": "в очереди",
    "kb.status.building": "сборка",
    "kb.status.ready": "готово",
    "kb.status.failed": "ошибка",
    "kb.status.none": "— нет БЗ —",
    "kb.cleared": "✅ База знаний для этого ключа очищена.",
    "kb.button.upload": "📤 Загрузить JSON",
    "kb.button.clear": "🗑 Очистить БЗ",
    "kb.upload.no_slot": (
        "⚠️ Сейчас не выбран API-ключ.\n"
        "Сначала откройте меню API, выберите ключ и нажмите кнопку «📚 БЗ»."
    ),
    "kb.upload.download_failed": "⚠️ Не удалось загрузить файл. Попробуйте ещё раз.",
    "kb.upload.read_failed": "⚠️ Не удалось прочитать файл. Попробуйте ещё раз.",
    "kb.upload.empty": "⚠️ Файл пустой или содержит только пробелы.",
    "kb.upload.bad_encoding": "⚠️ Некорректная кодировка. Отправьте валидный JSON-файл в UTF-8.",
    "kb.upload.bad_json": "⚠️ Некорректный JSON. Отправьте валидный JSON-файл в UTF-8.",
    "kb.upload.expect_array": "⚠️ Ожидался JSON-массив объектов.",
    "kb.upload.no_items": "⚠️ В JSON-файле не найдено ни одного подходящего элемента.",
    "kb.upload.too_large": "⚠️ JSON-файл слишком большой для загрузки.",
    "kb.upload.accepted": "✅ Файл базы знаний принят.\nСкоро я пересоберу индекс для этого ключа.",
    "kb.upload.hint": (
        "📚 Отправьте JSON-файл с массивом объектов.\n"
        "Обязательные поля: id, text, tags.\n"
        "Я пересоберу базу знаний для этого API-ключа."
    ),
    "kb.upload.truncated": "ℹ️ Примечание: импортировано {kept} из {total} элементов (лимит).",

    # Memory
    "memory.clear.confirm": (
        "⚠️ Это сотрёт память персоны о тебе.\n"
        "Отменить это действие нельзя.\n"
        "Продолжить?"
    ),
    "memory.clear.confirm_yes": "✅ Да",
    "memory.clear.confirm_no": "◀ Назад",
    "memory.clear.done": "✅ Готово. Память персоны очищена.",
    "memory.clear.cancelled": "❎ Отменено.",
}


def _assert_same_keys(a: Dict[str, str], b: Dict[str, str]) -> None:
    ka, kb = set(a.keys()), set(b.keys())
    only_a = sorted(ka - kb)
    only_b = sorted(kb - ka)
    if only_a or only_b:
        raise RuntimeError(
            "i18n key mismatch:\n"
            f"only in EN: {only_a}\n"
            f"only in RU: {only_b}"
        )


_assert_same_keys(_EN, _RU)

# Export
MESSAGES: Dict[Lang, Dict[str, str]] = {
    "en": dict(sorted(_EN.items())),
    "ru": dict(sorted(_RU.items())),
}