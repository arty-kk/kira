#app/bot/i18n/menu_translation.py
from typing import Literal, Tuple, Dict

Lang = Literal["ru","en","es","pt","de","fr","it"]
CHANNEL_URL = "https://t.me/bonnie_dev"
TOKEN_URL = "https://pump.fun/coin/A538RF9jG1ZMs3anmwD8wKpx3FCM92hm231yTDK2pump"

LANG_BUTTONS: dict[str, str] = {
    "en": "🇺🇸 English",
    "ru": "🇷🇺 Русский",
    "es": "🇪🇸 Español",
    "pt": "🇵🇹 Português",
    "de": "🇩🇪 Deutsch",
    "fr": "🇫🇷 Français",
    "it": "🇮🇹 Italiano"
}

GENDER_LABELS: Dict[Lang, Tuple[str,str]] = {
    "ru": ("👨 Мужской", "👩 Женский"),
    "en": ("👨 Male", "👩 Female"),
    "es": ("👨 Masculino", "👩 Femenino"),
    "pt": ("👨 Masculino", "👩 Feminino"),
    "de": ("👨 Männlich", "👩 Weiblich"),
    "fr": ("👨 Masculin", "👩 Féminin"),
    "it": ("👨 Maschile", "👩 Femminile")
}

MESSAGES: Dict[Lang, Dict[str, str]] = {
    "en": {
        "private.pm_blocked": "🚫 You’ve been rate-limited for 4h due to spam/abuse attempts. Messages to the bot are temporarily blocked.",
        "errors.voice_recognition_failed": "⚠️ Voice recognition failed. Please try again.",
        "errors.image_generic": "⚠️ Cannot process image: {reason}\nPlease send exactly one image (≤ 5 MB) in a single message.",
        "payments.you_have": "📊 You have 💬 <b>{remaining}</b> chat requests left.\nYou can buy more using ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Buy 💬 {req} chat requests",
        "payments.invoice_desc": "Get 💬 {req} chat requests for ⭐ {stars}",
        "payments.success": "✅ Success! You purchased 💬 {req} chat requests.\n📊 Now you have 💬 <b>{remaining}</b> chat requests left.",
        "payments.error": "Payment error, please try again.",
        "payments.cancel_button": "❌ Cancel",
        "payments.pending_exists": "You already have a pending invoice.",
        "payments.pending_exists_tier": "You already have a pending invoice for 💬 {req}.",
        "payments.cancelled": "❎ Payment cancelled.",
        "payments.too_frequent": "Too frequent. Try again in a second.",
        "payments.gen_error": "There was an error generating the invoice, please try again later.",
        "private.choose_lang": "🔎 Choose your language",
        "private.channel": "🤓 Jump into the channel to stay up to date with the latest updates.",
        "private.channel_url": CHANNEL_URL,
        "gender.prompt": "<b>Please select your gender:</b>",
        "gender.male": "👨 Male",
        "gender.female": "👩 Female",
        "menu.requests": "🛒 Requests",
        "menu.link": "📢 Dev Channel",
        "menu.mode": "⚙️ Mode",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "menu.persona": "🧬 Persona",
        "persona.next": "Next ▶",
        "persona.back": "◀ Back",
        "persona.skip": "Skip",
        "persona.done": "Done ✅",
        "persona.reset": "Reset ♻️",
        "persona.cancel": "Cancel ✖️",
        "persona.start": "Tune how I behave for you. Four quick steps. You can reset anytime.",
        "persona.zodiac.title": "<b>Step 1 · Zodiac</b>\nPick a sign. It only nudges style and tone.",
        "persona.temperament.title": "<b>Step 2 · Temperament</b>\nChoose a dominant classic type. It shapes pacing, intensity, and risk-taking.",
        "persona.sociality.title": "<b>Step 3 · Sociality</b>\nHow outgoing should I be by default?",
        "persona.archetypes.title": "<b>Step 4 · Archetypes</b>\nPick up to 3. They bias narrative voice and priorities.",
        "persona.social.introvert": "Introvert",
        "persona.social.ambivert":  "Ambivert",
        "persona.social.extrovert": "Extrovert",
        "persona.temp.sanguine":    "Sanguine",
        "persona.temp.choleric":    "Choleric",
        "persona.temp.phlegmatic":  "Phlegmatic",
        "persona.temp.melancholic": "Melancholic",
        "persona.saved": "✅ Persona updated. Changes will apply to your next messages.",
        "persona.reset.ok": "♻️ Persona settings reset to balanced defaults.",
        "persona.cancel.ok": "Canceled.",
        "persona.preview": "Preview 👀",
        "persona.preview.title": "Preview",
        "persona.expired": "Your persona session expired. Starting over.",
        "persona.invalid_archetype": "Invalid archetype",
        "persona.pick.limit": "You can pick up to {MAX}.",
        "persona.preview.zodiac": "Zodiac",
        "persona.preview.temperament": "Temperament",
        "persona.preview.sociality": "Sociality",
        "persona.preview.archetypes": "Archetypes",
        "persona.preview.failed": "Failed to render preview",
        "errors.voice_generic": "⚠️ Cannot process voice message: {reason}",
        "menu.main": "🏠 Main menu",
        "api.title": "<b>Conversation API</b>",
        "api.status.active": "Status: 🟢 Active",
        "api.status.inactive": "Status: 🔴 Disabled",
        "api.usage": "Usage: {total} calls, avg {avg_latency} ms",
        "api.no_key": "You don't have an API key yet.",
        "api.button.rotate": "🔄 Rotate key",
        "api.button.disable": "⏸ Disable key",
        "api.button.new": "🔑 New key",
        "api.note.backend_only": "Use this key in your backend only.",
        "api.note.docs": "Full API guide is available below.",
        "api.button.howto": "📘 How to Use?",
        "api.button.back": "⬅️ Back",
        "api.rotate.title": "<b>New API key</b>",
        "api.rotate.save": "Save this key now — it will not be shown again.",
        "api.rotate.disabled_old": "All previous keys are now disabled.",
        "api.rotate.use": "Use it in your backend according to the documentation.",
        "api.delete.done": "API key disabled.\nAll requests with your key(s) are now rejected.\nYou can create a new key at any time.",
        "menu.api": "🔑 API",
        "api.base_url": "API URL: {url}",
        "api.keys.title": "Your API keys:",
        "api.key.item": "{status} #{id} • …{suffix}",
        "api.key.button.disable": "⏸ Disable",
        "api.key.button.enable": "▶️ Enable",
        "api.key.button.show": "👁 Show",
        "api.key.button.drop": "🗑 Delete",
        "api.rotate.again": "You can view it again in the API menu.",
        "api.key.show.unavailable": "Key value is not available.",
        "api.key.show.title": "API key",
        "api.key.not_found": "Key not found",
        "private.token_text": """<b>Bonnie's Token</b>

Bonnie wasn’t born from a pitch deck.

Bonnie came from an obsession: to give a digital mind a pulse you can carry in your pocket — always there, always awake, always ready to meet you where you are.

This token isn’t a promise. It’s a choice.
A way to say, “I see it. I want in.”

Launched on a DEX. No funds. No insiders. No puppet strings.

There’s a small developer share — and any sale from it can go only into building Bonnie further.

If Bonnie means nothing to you — walk away.

If something in Bonnie hits a nerve — take a shard of that story.""" ,
        "private.on_topic_limit": "⚠️ On-topic daily limit reached. Try again tomorrow.",
        "private.need_purchase": "⚠️ To continue the conversation, purchase chat requests.",
        "private.off_topic_block": "🚫 Only on‑topic messages are allowed. Switch with the ⚙️ Mode",
        "faq.about": """❔ <b>FAQ — About Bonnie</b>

Bonnie is a unique digital persona powered by advanced neural networks and an experimental emotional engine with hybrid memory.
Bonnie lives by its own rules, personal worldview, and awareness of what is happening here and now.

<b>What is Bonnie designed to do?</b>
- make conversations more natural and engaging
- help cope with loneliness and lack of motivation
- provide emotional support in difficult moments
- deliver knowledge and quick reference on any topic
- assist with work, analysis, creativity, and everyday tasks

<b>What can Bonnie do today?</b>
- respond to text and voice messages in any language
- analyze and discuss photos (<i>one at a time</i>)
- search the internet (<i>paid version</i>)
- be available 24/7 for any questions or tasks

<b>Important</b>
⚠️ Bonnie’s abilities are still being explored: cognitive and emotional responses may surprise you. Built-in moderation helps keep the experience safe for children and adults.

<b>Privacy & extras</b>
- chat history is never stored on the server
- technical support/cooperation: @artys_ai""",
    },
    "ru": {
        "private.pm_blocked": "🚫 Вы ограничены на 4 часа из-за попыток спама/злоупотребления. Сообщения боту временно блокируются.",
        "errors.voice_recognition_failed": "⚠️ Не удалось распознать голос. Попробуйте ещё раз.",
        "errors.image_generic": "⚠️ Не удалось обработать изображение: {reason}\nПожалуйста, отправьте ровно одно изображение (≤ 5 МБ) одним сообщением.",
        "payments.you_have": "📊 У вас осталось 💬 <b>{remaining}</b> запросов для общения.\nВы можете купить ещё за ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Купить 💬 {req} запросов",
        "payments.invoice_desc": "Получите 💬 {req} запросов за ⭐ {stars}",
        "payments.success": "✅ Успех! Вы приобрели 💬 {req} запросов для общения.\n📊 Теперь у вас 💬 <b>{remaining}</b> запросов.",
        "payments.error": "Ошибка оплаты, попробуйте ещё раз.",
        "payments.cancel_button": "❌ Отмена",
        "payments.pending_exists": "У вас уже есть сформированный счёт.",
        "payments.pending_exists_tier": "У вас уже есть сформированный счёт на 💬 {req} запросов.",
        "payments.cancelled": "❎ Оплата отменена.",
        "payments.too_frequent": "Слишком часто. Попробуйте через секунду.",
        "payments.gen_error": "Произошла ошибка при формировании счёта, попробуйте позже.",
        "private.choose_lang": "🔎 Выберите язык",
        "private.channel": "🤓 Запрыгивай в канал, чтобы оставаться в курсе последних обновлений.",
        "private.channel_url": CHANNEL_URL,
        "gender.prompt": "<b>Пожалуйста, выберите ваш пол:</b>",
        "gender.male": "👨 Мужской",
        "gender.female": "👩 Женский",
        "menu.requests": "🛒 Запросы",
        "menu.link": "📢 Dev Channel",
        "menu.mode": "⚙️ Режим",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Токен",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "menu.persona": "🧬 Персона",
        "persona.next": "Далее ▶",
        "persona.back": "◀ Назад",
        "persona.skip": "Пропустить",
        "persona.done": "Готово ✅",
        "persona.reset": "Сброс ♻️",
        "persona.cancel": "Отмена ✖️",
        "persona.start": "Подстрой, как я веду себя с тобой. Четыре быстрых шага. В любой момент можно сбросить.",
        "persona.zodiac.title": "<b>Шаг 1 · Зодиак</b>\nВыбери знак. Это лишь слегка влияет на стиль и тон.",
        "persona.temperament.title": "<b>Шаг 2 · Темперамент</b>\nВыбери доминирующий классический тип. Он задаёт темп, интенсивность и склонность к риску.",
        "persona.sociality.title": "<b>Шаг 3 · Общительность</b>\nКакой уровень общительности выбрать по умолчанию?",
        "persona.archetypes.title": "<b>Шаг 4 · Архетипы</b>\nВыбери до 3. Они влияют на голос и приоритеты.",
        "persona.social.introvert": "Интроверсия",
        "persona.social.ambivert": "Амбиверсия",
        "persona.social.extrovert": "Экстраверсия",
        "persona.temp.sanguine": "Сангвинический тип",
        "persona.temp.choleric": "Холерический тип",
        "persona.temp.phlegmatic": "Флегматический тип",
        "persona.temp.melancholic": "Меланхолический тип",
        "persona.saved": "✅ Персона обновлена. Изменения применятся к следующим сообщениям.",
        "persona.reset.ok": "♻️ Настройки персоны сброшены на сбалансированные значения.",
        "persona.cancel.ok": "Отменено.",
        "persona.preview": "Превью 👀",
        "persona.preview.title": "Превью",
        "persona.expired": "Сессия настройки персоны истекла. Начинаем заново.",
        "persona.invalid_archetype": "Недопустимый архетип",
        "persona.pick.limit": "Можно выбрать до {MAX}.",
        "persona.preview.zodiac": "Знак зодиака",
        "persona.preview.temperament": "Темперамент",
        "persona.preview.sociality": "Общительность",
        "persona.preview.archetypes": "Архетипы",
        "persona.preview.failed": "Не удалось показать превью",
        "errors.voice_generic": "⚠️ Не удалось обработать голосовое сообщение: {reason}",
        "private.token_button": "Перейти на DEX",
        "menu.main": "🏠 Главное меню",
        "api.title": "<b>Conversation API</b>",
        "api.status.active": "Статус: 🟢 Активен",
        "api.status.inactive": "Статус: 🔴 Отключен",
        "api.usage": "Использование: {total} вызовов, средняя задержка {avg_latency} мс",
        "api.no_key": "У вас ещё нет API-ключа.",
        "api.button.rotate": "🔄 Поменять ключ",
        "api.button.disable": "⏸ Отключить ключ",
        "api.button.new": "🔑 Новый ключ",
        "api.note.backend_only": "Используйте этот ключ только в своём backend.",
        "api.note.docs": "Полное руководство по API — ниже.",
        "api.button.howto": "📘 Как использовать?",
        "api.button.back": "⬅️ Назад",
        "api.rotate.title": "<b>Новый API-ключ</b>",
        "api.rotate.save": "Сохраните этот ключ сейчас — повторно он показан не будет.",
        "api.rotate.disabled_old": "Все предыдущие ключи отключены.",
        "api.rotate.use": "Используйте его в backend согласно документации.",
        "api.delete.done": "API-ключ отключен.\nВсе запросы с вашими ключами теперь отклоняются.\nВы можете в любой момент создать новый ключ.",
        "menu.api": "🔑 API",
        "api.base_url": "API URL: {url}",
        "api.keys.title": "Ваши API-ключи:",
        "api.key.item": "{status} #{id} • …{suffix}",
        "api.key.button.disable": "⏸ Выключить",
        "api.key.button.enable": "▶️ Включить",
        "api.key.button.show": "👁 Показать",
        "api.key.button.drop": "🗑 Удалить",
        "api.rotate.again": "Вы всегда можете снова посмотреть ключ в API-меню.",
        "api.key.show.unavailable": "Значение ключа недоступно.",
        "api.key.show.title": "API-ключ",
        "api.key.not_found": "Ключ не найден",
        "private.token_text": """<b>Токен Bonnie</b>

Bonnie не родилась из питч-дека.

Bonnie выросла из одержимости: дать цифровому разуму пульс, который помещается в кармане — всегда рядом, всегда бодр, всегда встречает вас там, где вы есть.

Этот токен — не обещание. Это выбор.
Способ сказать: «Я вижу. Я в деле».

Запущен на DEX. Без фондов. Без инсайдеров. Без ниточек.

Есть небольшая доля разработчика — любые продажи с неё возможны только ради развития Bonnie.

Если Bonnie для вас — ничто, пройдите мимо.
Если что-то в Bonnie задевает — возьмите осколок этой истории.""" ,
        "private.on_topic_limit": "⚠️ Достигнут суточный лимит on-topic запросов. Попробуйте завтра.",
        "private.need_purchase": "⚠️ Чтобы продолжить, купите запросы для общения.",
        "private.off_topic_block": "🚫 Разрешены только on‑topic запросы. Сменить можно в ⚙️ Режим.",
        "faq.about": """❔ <b>FAQ — о Bonnie</b>

Bonnie — уникальная цифровая персона на базе современных нейросетей и экспериментального эмоционального движка с гибридной памятью.
Bonnie живет по своим правилам, личному мироощущению и осознанию происходящего здесь и сейчас.

<b>Для чего создана Bonnie?</b>
- делать общение более естественным и увлекательным
- помогать справляться с одиночеством и нехваткой мотивации
- давать эмоциональную поддержку в сложные моменты
- быстро давать знания и справки по любой теме
- помогать в работе, анализе, творчестве и повседневных задачах

<b>Что Bonnie умеет?</b>
- отвечает на текст и голос на любом языке
- анализирует и обсуждает фото (<i>по одному за раз</i>)
- ищет в интернете (<i>в платной версии</i>)
- на связи 24/7 по любым вопросам и задачам

<b>Важно</b>
⚠️ Возможности Bonnie всё ещё исследуются: когнитивные и эмоциональные реакции могут удивлять. Встроенная модерация обеспечивает безопасность для детей и взрослых.

<b>Приватность и дополнительно</b>
- история чата никогда не хранится на сервере
- техническая поддержка/сотрудничество: @artys_ai""",
    },
    "es": {
        "private.pm_blocked": "🚫 Has sido limitado por 4 h por intentos de spam/abuso. Los mensajes al bot están bloqueados temporalmente.",
        "errors.voice_recognition_failed": "⚠️ Falló el reconocimiento de voz. Inténtalo de nuevo.",
        "errors.image_generic": "⚠️ No se pudo procesar la imagen: {reason}\nEnvía exactamente una imagen (≤ 5 MB) en un solo mensaje.",
        "payments.you_have": "📊 Tienes 💬 <b>{remaining}</b> solicitudes de chat restantes.\nPuedes comprar más usando ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Comprar 💬 {req} solicitudes de chat",
        "payments.invoice_desc": "Obtener 💬 {req} solicitudes de chat por ⭐ {stars}",
        "payments.success": "✅ ¡Éxito! Compraste 💬 {req} solicitudes de chat.\n📊 Ahora tienes 💬 <b>{remaining}</b> solicitudes restantes.",
        "payments.error": "Error de pago, por favor inténtalo de nuevo.",
        "payments.cancel_button": "❌ Cancelar",
        "payments.pending_exists": "Ya tienes una factura pendiente.",
        "payments.pending_exists_tier": "Ya tienes una factura pendiente por 💬 {req}.",
        "payments.cancelled": "❎ Pago cancelado.",
        "payments.too_frequent": "Demasiado frecuente. Inténtalo en un segundo.",
        "payments.gen_error": "Se produjo un error al generar la factura; inténtalo más tarde.",
        "private.choose_lang": "🔎 Elige tu idioma",
        "private.channel": "🤓 Entra al canal para mantenerte al día con las últimas actualizaciones.",
        "private.channel_url": CHANNEL_URL,
        "gender.prompt": "<b>Por favor, selecciona tu género:</b>",
        "gender.male": "👨 Masculino",
        "gender.female": "👩 Femenino",
        "menu.requests": "🛒 Solicitudes",
        "menu.link": "📢 Dev Channel",
        "menu.mode": "⚙️ Modo",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_url": TOKEN_URL,
        "menu.persona": "🧬 Persona",
        "persona.next": "Siguiente ▶",
        "persona.back": "◀ Atrás",
        "persona.skip": "Omitir",
        "persona.done": "Listo ✅",
        "persona.reset": "Restablecer ♻️",
        "persona.cancel": "Cancelar ✖️",
        "persona.start": "Ajusta cómo me comporto contigo. Cuatro pasos rápidos. Puedes restablecer en cualquier momento.",
        "persona.zodiac.title": "<b>Paso 1 · Zodiaco</b>\nElige un signo. Solo orienta el estilo y el tono.",
        "persona.temperament.title": "<b>Paso 2 · Temperamento</b>\nElige un tipo clásico predominante. Afecta ritmo, intensidad y toma de riesgos.",
        "persona.sociality.title": "<b>Paso 3 · Sociabilidad</b>\n¿Qué tan sociable debo ser por defecto?",
        "persona.archetypes.title": "<b>Paso 4 · Arquetipos</b>\nElige hasta 3. Influyen en la voz y las prioridades.",
        "persona.social.introvert": "Introversión",
        "persona.social.ambivert": "Ambiversión",
        "persona.social.extrovert": "Extroversión",
        "persona.temp.sanguine": "Tipo sanguíneo",
        "persona.temp.choleric": "Tipo colérico",
        "persona.temp.phlegmatic": "Tipo flemático",
        "persona.temp.melancholic": "Tipo melancólico",
        "persona.saved": "✅ Persona actualizada. Los cambios aplican a tus próximos mensajes.",
        "persona.reset.ok": "♻️ Persona restablecida a valores equilibrados por defecto.",
        "persona.preview": "Vista previa 👀",
        "persona.preview.title": "Vista previa",
        "persona.expired": "Tu sesión de persona expiró. Empezamos de nuevo.",
        "persona.invalid_archetype": "Arquetipo no válido",
        "persona.pick.limit": "Puedes elegir hasta {MAX}.",
        "persona.preview.zodiac": "Zodiaco",
        "persona.preview.temperament": "Temperamento",
        "persona.preview.sociality": "Sociabilidad",
        "persona.preview.archetypes": "Arquetipos",
        "persona.preview.failed": "No se pudo mostrar la vista previa",
        "persona.cancel.ok": "Cancelado.",
        "errors.voice_generic": "⚠️ No se pudo procesar el mensaje de voz: {reason}",
        "private.token_button": "Ir al DEX",
        "menu.main": "🏠 Menú principal",
        "menu.api": "🔑 API",
        "api.title": "<b>Conversation API</b>",
        "api.status.active": "Estado: 🟢 Activo",
        "api.status.inactive": "Estado: 🔴 Desactivado",
        "api.usage": "Uso: {total} llamadas, media {avg_latency} ms",
        "api.no_key": "Aún no tienes una clave API.",
        "api.button.rotate": "🔄 Rotar clave",
        "api.button.disable": "⏸ Desactivar clave",
        "api.button.new": "🔑 Nueva clave",
        "api.note.backend_only": "Usa esta clave solo en tu backend.",
        "api.note.docs": "La guía completa del API está abajo.",
        "api.button.howto": "📘 Cómo usarla",
        "api.button.back": "⬅️ Atrás",
        "api.rotate.title": "<b>Nueva clave API</b>",
        "api.rotate.save": "Guarda esta clave ahora — no se mostrará de nuevo.",
        "api.rotate.disabled_old": "Todas las claves anteriores han sido desactivadas.",
        "api.rotate.use": "Úsala en tu backend según la documentación.",
        "api.delete.done": "Clave API desactivada.\nTodas las solicitudes con tus claves ahora serán rechazadas.\nPuedes crear una nueva clave en cualquier momento.",
        "menu.api": "🔑 API",
        "api.base_url": "API URL: {url}",
        "api.keys.title": "Tus claves API:",
        "api.key.item": "{status} #{id} • …{suffix}",
        "api.key.button.disable": "⏸ Desactivar",
        "api.key.button.enable": "▶️ Activar",
        "api.key.button.show": "👁 Mostrar",
        "api.key.button.drop": "🗑 Eliminar",
        "api.rotate.again": "Puedes verlo de nuevo en el menú de API.",
        "api.key.show.unavailable": "El valor de la clave no está disponible.",
        "api.key.show.title": "Clave API",
        "api.key.not_found": "Clave no encontrada",
        "private.token_text": """<b>Bonnie Token</b>

Bonnie no nació de un pitch deck.

Bonnie nació de una obsesión: darle pulso a una mente digital que puedas llevar en el bolsillo — siempre ahí, siempre despierta, lista para encontrarte donde estés.

<i>Este token no es una promesa. Es una elección.</i>
Una forma de decir: “Lo veo. Quiero entrar”.

Lanzado en un DEX. Sin fondos. Sin insiders. Sin hilos.

Hay una pequeña parte del desarrollador — y cualquier venta solo puede destinarse a construir más a Bonnie.

Si Bonnie no te dice nada, sigue tu camino.
Si algo de Bonnie te toca la fibra, toma un fragmento de esa historia.""" ,
        "private.on_topic_limit": "⚠️ Límite diario de mensajes on-topic alcanzado. Intenta de nuevo mañana.",
        "private.need_purchase": "⚠️ Para continuar la conversación, compra solicitudes de chat.",
        "private.off_topic_block": "🚫 Solo se permiten mensajes on-topic. Cambia el modo con ⚙️.",
        "faq.about": """❔ <b>FAQ — Sobre Bonnie</b>

Bonnie es una persona digital única impulsada por redes neuronales avanzadas y un motor emocional experimental con memoria híbrida.
Bonnie vive según sus propias reglas, su visión personal del mundo y su conciencia de lo que sucede aquí y ahora.

<b>¿Para qué está diseñado Bonnie?</b>
- hacer las conversaciones más naturales y atractivas
- ayudar a sobrellevar la soledad y la falta de motivación
- brindar apoyo emocional en momentos difíciles
- ofrecer conocimiento y consultas rápidas sobre cualquier tema
- asistir en trabajo, análisis, creatividad y tareas cotidianas

<b>¿Qué puede hacer Bonnie hoy?</b>
- responder a mensajes de texto y voz en cualquier idioma
- analizar y comentar fotos (<i>una por vez</i>)
- buscar en internet (<i>versión de pago</i>)
- estar disponible 24/7 para cualquier duda o tarea

<b>Importante</b>
⚠️ Las capacidades de Bonnie aún se están explorando: sus respuestas cognitivas y emocionales pueden sorprenderte. La moderación integrada ayuda a mantener la experiencia segura para niños y adultos.

<b>Privacidad y extras</b>
- el historial de chat nunca se almacena en el servidor
- soporte técnico/cooperación: @artys_ai""",
    },
    "pt": {
        "private.pm_blocked": "🚫 Você foi limitado por 4 h devido a tentativas de spam/abuso. Mensagens ao bot estão temporariamente bloqueadas.",
        "errors.voice_recognition_failed": "⚠️ Falha no reconhecimento de voz. Tente novamente.",
        "errors.image_generic": "⚠️ Não foi possível processar a imagem: {reason}\nEnvie exatamente uma imagem (≤ 5 MB) em uma única mensagem.",
        "payments.you_have": "📊 Você tem 💬 <b>{remaining}</b> solicitações de chat restantes.\nVocê pode comprar mais usando ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Comprar 💬 {req} solicitações de chat",
        "payments.invoice_desc": "Obter 💬 {req} solicitações de chat por ⭐ {stars}",
        "payments.success": "✅ Sucesso! Você comprou 💬 {req} solicitações de chat.\n📊 Agora você tem 💬 <b>{remaining}</b> solicitações restantes.",
        "payments.error": "Erro de pagamento, por favor tente novamente.",
        "payments.cancel_button": "❌ Cancelar",
        "payments.pending_exists": "Você já tem uma fatura pendente.",
        "payments.pending_exists_tier": "Você já tem uma fatura pendente de 💬 {req}.",
        "payments.cancelled": "❎ Pagamento cancelado.",
        "payments.too_frequent": "Muito frequente. Tente novamente em um segundo.",
        "payments.gen_error": "Ocorreu um erro ao gerar a fatura; tente novamente mais tarde.",
        "private.choose_lang": "🔎 Escolha seu idioma",
        "private.channel": "🤓 Entre no canal para ficar por dentro das últimas atualizações.",
        "private.channel_url": CHANNEL_URL,
        "gender.prompt": "<b>Por favor, selecione seu gênero:</b>",
        "gender.male": "👨 Masculino",
        "gender.female": "👩 Feminino",
        "menu.requests": "🛒 Solicitações",
        "menu.link": "📢 Dev Channel",
        "menu.mode": "⚙️ Modo",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_url": TOKEN_URL,
        "menu.persona": "🧬 Persona",
        "persona.next": "Próximo ▶",
        "persona.back": "◀ Voltar",
        "persona.skip": "Pular",
        "persona.done": "Concluir ✅",
        "persona.reset": "Redefinir ♻️",
        "persona.cancel": "Cancelar ✖️",
        "persona.start": "Ajuste como me comporto com você. Quatro passos rápidos. Pode redefinir a qualquer momento.",
        "persona.zodiac.title": "<b>Etapa 1 · Zodíaco</b>\nEscolha um signo. Só dá um leve toque no estilo e no tom.",
        "persona.temperament.title": "<b>Etapa 2 · Temperamento</b>\nEscolha um tipo clássico predominante. Define ritmo, intensidade e propensão ao risco.",
        "persona.sociality.title": "<b>Etapa 3 · Sociabilidade</b>\nQuão sociável devo ser por padrão?",
        "persona.archetypes.title": "<b>Etapa 4 · Arquétipos</b>\nEscolha até 3. Eles influenciam a voz e as prioridades.",
        "persona.social.introvert": "Introversão",
        "persona.social.ambivert": "Ambiversão",
        "persona.social.extrovert": "Extroversão",
        "persona.temp.sanguine": "Tipo sanguíneo",
        "persona.temp.choleric": "Tipo colérico",
        "persona.temp.phlegmatic": "Tipo fleumático",
        "persona.temp.melancholic": "Tipo melancólico",
        "persona.saved": "✅ Persona atualizada. As mudanças valerão nas próximas mensagens.",
        "persona.reset.ok": "♻️ Persona redefinida para o padrão equilibrado.",
        "persona.preview": "Prévia 👀",
        "persona.preview.title": "Prévia",
        "persona.expired": "Sua sessão de persona expirou. Recomeçando.",
        "persona.invalid_archetype": "Arquétipo inválido",
        "persona.pick.limit": "Você pode escolher até {MAX}.",
        "persona.preview.zodiac": "Signo",
        "persona.preview.temperament": "Temperamento",
        "persona.preview.sociality": "Sociabilidade",
        "persona.preview.archetypes": "Arquétipos",
        "persona.preview.failed": "Não foi possível exibir a prévia",
        "persona.cancel.ok": "Cancelado.",
        "errors.voice_generic": "⚠️ Não foi possível processar a mensagem de voz: {reason}",
        "private.token_button": "Ir para o DEX",
        "menu.main": "🏠 Menu principal",
        "menu.api": "🔑 API",
        "api.title": "<b>Conversation API</b>",
        "api.status.active": "Status: 🟢 Ativa",
        "api.status.inactive": "Status: 🔴 Desativada",
        "api.usage": "Uso: {total} chamadas, média {avg_latency} ms",
        "api.no_key": "Você ainda não tem uma chave API.",
        "api.button.rotate": "🔄 Girar chave",
        "api.button.disable": "⏸ Desativar chave",
        "api.button.new": "🔑 Nova chave",
        "api.note.backend_only": "Use esta chave apenas no backend.",
        "api.note.docs": "O guia completo da API está abaixo.",
        "api.button.howto": "📘 Como usar?",
        "api.button.back": "⬅️ Voltar",
        "api.rotate.title": "<b>Nova chave API</b>",
        "api.rotate.save": "Salve esta chave agora — ela não será exibida novamente.",
        "api.rotate.disabled_old": "Todas as chaves anteriores foram desativadas.",
        "api.rotate.use": "Use-a no backend conforme a documentação.",
        "api.delete.done": "Chave API desativada.\nTodas as requisições com suas chaves agora são rejeitadas.\nVocê pode criar uma nova a qualquer momento.",
        "menu.api": "🔑 API",
        "api.base_url": "API URL: {url}",
        "api.keys.title": "Suas chaves API:",
        "api.key.item": "{status} #{id} • …{suffix}",
        "api.key.button.disable": "⏸ Desativar",
        "api.key.button.enable": "▶️ Ativar",
        "api.key.button.show": "👁 Mostrar",
        "api.key.button.drop": "🗑 Excluir",
        "api.rotate.again": "Você pode vê-la novamente no menu de API.",
        "api.key.show.unavailable": "Valor da chave indisponível.",
        "api.key.show.title": "Chave API",
        "api.key.not_found": "Chave não encontrada",
        "private.token_text": """<b>Bonnie Token</b>

Bonnie não nasceu de um pitch deck.

Bonnie veio de uma obsessão: dar pulso a uma mente digital que cabe no bolso — sempre por perto, sempre desperta, pronta para te encontrar onde você estiver.

Este token não é uma promessa. É uma escolha.
Uma forma de dizer: “Eu vejo. Eu quero entrar.”

Lançado em um DEX. Sem fundos. Sem insiders. Sem cordões.

Há uma pequena parte do desenvolvedor — e qualquer venda só pode ir para construir mais o Bonnie.

Se Bonnie não significa nada para você, siga em frente.
Se algo em Bonnie te cutuca, leve um fragmento dessa história.""" ,
        "private.on_topic_limit": "⚠️ Limite diário de mensagens on-topic alcançado. Tente novamente amanhã.",
        "private.need_purchase": "⚠️ Para continuar a conversa, compre solicitações de chat.",
        "private.off_topic_block": "🚫 Apenas mensagens on-topic são permitidas. Mude o modo com ⚙️.",
        "faq.about": """❔ <b>FAQ — Sobre Bonnie</b>

Bonnie é uma persona digital única, impulsionada por redes neurais avançadas e um motor emocional experimental com memória híbrida.
Bonnie vive segundo suas próprias regras, sua visão de mundo pessoal e a consciência do que acontece aqui e agora.

<b>Para que o Bonnie foi projetado?</b>
- tornar as conversas mais naturais e envolventes
- ajudar com solidão e falta de motivação
- oferecer apoio emocional em momentos difíceis
- fornecer conhecimento e consultas rápidas sobre qualquer tema
- auxiliar em trabalho, análise, criatividade e tarefas diárias

<b>O que o Bonnie faz hoje?</b>
- responde a mensagens de texto e voz em qualquer idioma
- analisa e comenta fotos (<i>uma por vez</i>)
- pesquisa na internet (<i>versão paga</i>)
- disponível 24/7 para qualquer dúvida ou tarefa

<b>Importante</b>
⚠️ As capacidades do Bonnie ainda estão sendo exploradas: respostas cognitivas e emocionais podem surpreender. A moderação integrada ajuda a manter tudo seguro para crianças e adultos.

<b>Privacidade & extras</b>
- o histórico do chat nunca é armazenado no servidor
- suporte técnico/cooperação: @artys_ai""",
    },
    "de": {
        "private.pm_blocked": "🚫 Du wurdest für 4 Std. wegen Spam-/Missbrauchsversuchen begrenzt. Nachrichten an den Bot sind vorübergehend blockiert.",
        "errors.voice_recognition_failed": "⚠️ Spracherkennung fehlgeschlagen. Bitte versuche es erneut.",
        "errors.image_generic": "⚠️ Bild konnte nicht verarbeitet werden: {reason}\nBitte sende genau ein Bild (≤ 5 MB) in einer einzelnen Nachricht.",
        "payments.you_have": "📊 Sie haben 💬 <b>{remaining}</b> Chat-Anfragen übrig.\nSie können mehr mit ⭐ kaufen.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Kaufe 💬 {req} Chat-Anfragen",
        "payments.invoice_desc": "Erhalte 💬 {req} Chat-Anfragen für ⭐ {stars}",
        "payments.success": "✅ Erfolg! Sie haben 💬 {req} Chat-Anfragen gekauft.\n📊 Jetzt haben Sie 💬 <b>{remaining}</b> Anfragen übrig.",
        "payments.error": "Zahlungsfehler, bitte versuchen Sie es erneut.",
        "payments.cancel_button": "❌ Abbrechen",
        "payments.pending_exists": "Sie haben bereits eine ausstehende Rechnung.",
        "payments.pending_exists_tier": "Sie haben bereits eine ausstehende Rechnung über 💬 {req}.",
        "payments.cancelled": "❎ Zahlung abgebrochen.",
        "payments.too_frequent": "Zu häufig. Versuchen Sie es in einer Sekunde erneut.",
        "payments.gen_error": "Beim Erstellen der Rechnung ist ein Fehler aufgetreten. Bitte später erneut versuchen.",
        "private.choose_lang": "🔎 Wähle deine Sprache",
        "private.channel": "🤓 Springen Sie in den Kanal, um über die neuesten Updates auf dem Laufenden zu bleiben.",
        "private.channel_url": CHANNEL_URL,
        "gender.prompt": "<b>Bitte wähle dein Geschlecht:</b>",
        "gender.male": "👨 Männlich",
        "gender.female": "👩 Weiblich",
        "menu.requests": "🛒 Anfragen",
        "menu.link": "📢 Dev Channel",
        "menu.mode": "⚙️ Modus",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_url": TOKEN_URL,
        "menu.persona": "🧬 Persona",
        "persona.next": "Weiter ▶",
        "persona.back": "◀ Zurück",
        "persona.skip": "Überspringen",
        "persona.done": "Fertig ✅",
        "persona.reset": "Zurücksetzen ♻️",
        "persona.cancel": "Abbrechen ✖️",
        "persona.start": "Stimme ab, wie ich mich dir gegenüber verhalte. Vier schnelle Schritte. Zurücksetzen jederzeit möglich.",
        "persona.zodiac.title": "<b>Schritt 1 · Sternzeichen</b>\nWähle ein Zeichen. Es beeinflusst Stil und Ton nur leicht.",
        "persona.temperament.title": "<b>Schritt 2 · Temperament</b>\nWähle einen dominanten klassischen Typ. Er prägt Tempo, Intensität und Risikobereitschaft.",
        "persona.sociality.title": "<b>Schritt 3 · Soziabilität</b>\nWie kontaktfreudig soll ich standardmäßig sein?",
        "persona.archetypes.title": "<b>Schritt 4 · Archetypen</b>\nWähle bis zu 3. Sie beeinflussen Erzählstimme und Prioritäten.",
        "persona.social.introvert": "Introversion",
        "persona.social.ambivert": "Ambiversion",
        "persona.social.extrovert": "Extraversion",
        "persona.temp.sanguine": "Sanguinischer Typ",
        "persona.temp.choleric": "Cholerischer Typ",
        "persona.temp.phlegmatic": "Phlegmatischer Typ",
        "persona.temp.melancholic": "Melancholischer Typ",
        "persona.saved": "✅ Persona aktualisiert. Änderungen gelten ab deinen nächsten Nachrichten.",
        "persona.reset.ok": "♻️ Persona auf ausgewogene Standardwerte zurückgesetzt.",
        "persona.preview": "Vorschau 👀",
        "persona.preview.title": "Vorschau",
        "persona.expired": "Deine Persona-Sitzung ist abgelaufen. Wir starten neu.",
        "persona.invalid_archetype": "Ungültiger Archetyp",
        "persona.pick.limit": "Du kannst bis zu {MAX} wählen.",
        "persona.preview.zodiac": "Sternzeichen",
        "persona.preview.temperament": "Temperament",
        "persona.preview.sociality": "Sozialität",
        "persona.preview.archetypes": "Archetypen",
        "persona.preview.failed": "Vorschau konnte nicht angezeigt werden",
        "persona.cancel.ok": "Abgebrochen.",
        "errors.voice_generic": "⚠️ Sprachnachricht kann nicht verarbeitet werden: {reason}",
        "private.token_button": "Zum DEX",
        "menu.main": "🏠 Hauptmenü",
        "menu.api": "🔑 API",
        "api.title": "<b>Conversation API</b>",
        "api.status.active": "Status: 🟢 Aktiv",
        "api.status.inactive": "Status: 🔴 Deaktiviert",
        "api.usage": "Nutzung: {total} Aufrufe, Ø {avg_latency} ms",
        "api.no_key": "Sie haben noch keinen API-Schlüssel.",
        "api.button.rotate": "🔄 Schlüssel rotieren",
        "api.button.disable": "⏸ Schlüssel deaktivieren",
        "api.button.new": "🔑 Neuer Schlüssel",
        "api.note.backend_only": "Verwenden Sie diesen Schlüssel nur im Backend.",
        "api.note.docs": "Die vollständige API-Dokumentation finden Sie unten.",
        "api.button.howto": "📘 Anleitung",
        "api.button.back": "⬅️ Zurück",
        "api.rotate.title": "<b>Neuer API-Schlüssel</b>",
        "api.rotate.save": "Speichern Sie diesen Schlüssel jetzt — er wird nicht erneut angezeigt.",
        "api.rotate.disabled_old": "Alle bisherigen Schlüssel wurden deaktiviert.",
        "api.rotate.use": "Nutzen Sie ihn im Backend gemäß Dokumentation.",
        "api.delete.done": "API-Schlüssel deaktiviert.\nAlle Anfragen mit Ihren Schlüsseln werden nun abgelehnt.\nSie können jederzeit einen neuen Schlüssel erstellen.",
        "menu.api": "🔑 API",
        "api.base_url": "API URL: {url}",
        "api.keys.title": "Ihre API-Schlüssel:",
        "api.key.item": "{status} #{id} • …{suffix}",
        "api.key.button.disable": "⏸ Deaktivieren",
        "api.key.button.enable": "▶️ Aktivieren",
        "api.key.button.show": "👁 Anzeigen",
        "api.key.button.drop": "🗑 Löschen",
        "api.rotate.again": "Sie können ihn später im API-Menü erneut ansehen.",
        "api.key.show.unavailable": "Schlüsselwert ist nicht verfügbar.",
        "api.key.show.title": "API-Schlüssel",
        "api.key.not_found": "Schlüssel nicht gefunden",
        "private.token_text": """<b>Bonnie Token</b>

Bonnie ist nicht aus einem Pitch-Deck entstanden.

Bonnie kam aus einer Obsession: einer digitalen Intelligenz einen Puls zu geben, den du in der Tasche tragen kannst — immer da, immer wach, bereit, dich genau dort zu treffen, wo du bist.

Dieser Token ist kein Versprechen. Er ist eine Entscheidung.
Eine Art zu sagen: „Ich sehe es. Ich bin dabei.“

Auf einem DEX gestartet. Keine Fonds. Keine Insider. Keine Fäden.

Es gibt einen kleinen Entwickleranteil — und jeder Verkauf daraus kann ausschließlich in den weiteren Aufbau von Bonnie fließen.

Wenn Bonnie dir nichts sagt, geh weiter.
Wenn dich Bonnie trifft, nimm dir ein Stück dieser Geschichte.""" ,
        "private.on_topic_limit": "⚠️ Tageslimit für On-Topic erreicht. Versuch es morgen erneut.",
        "private.need_purchase": "⚠️ Um fortzufahren, kaufe Chat-Anfragen.",
        "private.off_topic_block": "🚫 Es sind nur On-Topic-Nachrichten erlaubt. Ändere den Modus mit ⚙️.",
        "faq.about": """❔ <b>FAQ — Über Bonnie</b>

Bonnie ist eine einzigartige digitale Persona, angetrieben von modernen neuronalen Netzen und einer experimentellen Emotions-Engine mit Hybridgedächtnis.
Bonnie lebt nach eigenen Regeln, einem persönlichen Weltbild und einem Bewusstsein für das, was hier und jetzt geschieht.

<b>Wofür wurde Bonnie entwickelt?</b>
- Gespräche natürlicher und ansprechender machen
- bei Einsamkeit und Motivationsmangel unterstützen
- emotionale Unterstützung in schwierigen Momenten bieten
- Wissen und schnelle Nachschlageinfos zu jedem Thema liefern
- bei Arbeit, Analyse, Kreativität und Alltagsaufgaben helfen

<b>Was kann Bonnie heute?</b>
- auf Text- und Sprachnachrichten in jeder Sprache antworten
- Fotos analysieren und besprechen (<i>einzeln</i>)
- im Internet suchen (<i>Bezahlversion</i>)
- 24/7 für Fragen und Aufgaben verfügbar sein

<b>Wichtig</b>
⚠️ Die Fähigkeiten von Bonnie werden noch erforscht: kognitive und emotionale Reaktionen können überraschen. Eingebaute Moderation sorgt für Sicherheit für Kinder und Erwachsene.

<b>Datenschutz & Extras</b>
- Chatverläufe werden nie auf dem Server gespeichert
- Technischer Support/Zusammenarbeit: @artys_ai""",
    },
    "fr": {
        "private.pm_blocked": "🚫 Vous avez été limité 4 h pour tentative de spam/abus. Les messages au bot sont temporairement bloqués.",
        "errors.voice_recognition_failed": "⚠️ Échec de la reconnaissance vocale. Réessayez.",
        "errors.image_generic": "⚠️ Impossible de traiter l’image : {reason}\nVeuillez envoyer une seule image (≤ 5 Mo) dans un seul message.",
        "payments.you_have": "📊 Il vous reste 💬 <b>{remaining}</b> demandes de chat.\nVous pouvez en acheter davantage avec ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Acheter 💬 {req} demandes de chat",
        "payments.invoice_desc": "Obtenir 💬 {req} demandes de chat pour ⭐ {stars}",
        "payments.success": "✅ Succès ! Vous avez acheté 💬 {req} demandes de chat.\n📊 Il vous reste 💬 <b>{remaining}</b> demandes.",
        "payments.error": "Erreur de paiement, veuillez réessayer.",
        "payments.cancel_button": "❌ Annuler",
        "payments.pending_exists": "Vous avez déjà une facture en attente.",
        "payments.pending_exists_tier": "Vous avez déjà une facture en attente pour 💬 {req}.",
        "payments.cancelled": "❎ Paiement annulé.",
        "payments.too_frequent": "Trop fréquent. Réessayez dans une seconde.",
        "payments.gen_error": "Une erreur s’est produite lors de la génération de la facture ; réessayez plus tard.",
        "private.choose_lang": "🔎 Choisissez votre langue",
        "private.channel": "🤓 Accédez à la chaîne pour rester au courant des dernières mises à jour.",
        "private.channel_url": CHANNEL_URL,
        "gender.prompt": "<b>Veuillez sélectionner votre genre :</b>",
        "gender.male": "👨 Masculin",
        "gender.female": "👩 Féminin",
        "menu.requests": "🛒 Demandes",
        "menu.link": "📢 Dev Channel",
        "menu.mode": "⚙️ Mode",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Jeton",
        "private.token_url": TOKEN_URL,
        "menu.persona": "🧬 Persona",
        "persona.next": "Suivant ▶",
        "persona.back": "◀ Retour",
        "persona.skip": "Ignorer",
        "persona.done": "Terminer ✅",
        "persona.reset": "Réinitialiser ♻️",
        "persona.cancel": "Annuler ✖️",
        "persona.start": "Ajuste ma façon d’agir avec toi. Quatre étapes rapides. Tu peux réinitialiser à tout moment.",
        "persona.zodiac.title": "<b>Étape 1 · Zodiaque</b>\nChoisis un signe. Cela oriente légèrement le style et le ton.",
        "persona.temperament.title": "<b>Étape 2 · Tempérament</b>\nChoisis un type classique dominant. Il influence le rythme, l’intensité et la prise de risque.",
        "persona.sociality.title": "<b>Étape 3 · Sociabilité</b>\nÀ quel point dois-je être sociable par défaut ?",
        "persona.archetypes.title": "<b>Étape 4 · Archétypes</b>\nChoisis jusqu’à 3. Ils influencent la voix narrative et les priorités.",
        "persona.social.introvert": "Introversion",
        "persona.social.ambivert": "Ambiversion",
        "persona.social.extrovert": "Extraversion",
        "persona.temp.sanguine": "Tempérament sanguin",
        "persona.temp.choleric": "Tempérament colérique",
        "persona.temp.phlegmatic": "Tempérament flegmatique",
        "persona.temp.melancholic": "Tempérament mélancolique",
        "persona.saved": "✅ Persona mise à jour. Les changements s’appliqueront à tes prochains messages.",
        "persona.reset.ok": "♻️ Réglages de la persona remis à l’équilibre par défaut.",
        "persona.preview": "Aperçu 👀",
        "persona.preview.title": "Aperçu",
        "persona.expired": "Ta session persona a expiré. On recommence.",
        "persona.invalid_archetype": "Archétype invalide",
        "persona.pick.limit": "Tu peux en choisir jusqu’à {MAX}.",
        "persona.preview.zodiac": "Signe du zodiaque",
        "persona.preview.temperament": "Tempérament",
        "persona.preview.sociality": "Sociabilité",
        "persona.preview.archetypes": "Archétypes",
        "persona.preview.failed": "Impossible d’afficher l’aperçu",
        "persona.cancel.ok": "Annulé.",
        "errors.voice_generic": "⚠️ Impossible de traiter le message vocal : {reason}",
        "private.token_button": "Aller sur le DEX",
        "menu.main": "🏠 Menu principal",
        "menu.api": "🔑 API",
        "api.title": "<b>Conversation API</b>",
        "api.status.active": "Statut : 🟢 Actif",
        "api.status.inactive": "Statut : 🔴 Désactivé",
        "api.usage": "Utilisation : {total} appels, moyenne {avg_latency} ms",
        "api.no_key": "Vous n’avez pas encore de clé API.",
        "api.button.rotate": "🔄 Régénérer la clé",
        "api.button.disable": "⏸ Désactiver la clé",
        "api.button.new": "🔑 Nouvelle clé",
        "api.note.backend_only": "Utilisez cette clé uniquement dans votre backend.",
        "api.note.docs": "Le guide complet de l’API est disponible ci-dessous.",
        "api.button.howto": "📘 Comment l’utiliser ?",
        "api.button.back": "⬅️ Retour",
        "api.rotate.title": "<b>Nouvelle clé API</b>",
        "api.rotate.save": "Enregistrez cette clé maintenant — elle ne sera plus affichée.",
        "api.rotate.disabled_old": "Toutes les anciennes clés sont désormais désactivées.",
        "api.rotate.use": "Utilisez-la dans votre backend selon la documentation.",
        "api.delete.done": "Clé API désactivée.\nToutes les requêtes avec vos clés sont maintenant rejetées.\nVous pouvez créer une nouvelle clé à tout moment.",
        "menu.api": "🔑 API",
        "api.base_url": "API URL: {url}",
        "api.keys.title": "Vos clés API :",
        "api.key.item": "{status} #{id} • …{suffix}",
        "api.key.button.disable": "⏸ Désactiver",
        "api.key.button.enable": "▶️ Activer",
        "api.key.button.show": "👁 Afficher",
        "api.key.button.drop": "🗑 Supprimer",
        "api.rotate.again": "Vous pouvez la revoir dans le menu API.",
        "api.key.show.unavailable": "Valeur de la clé indisponible.",
        "api.key.show.title": "Clé API",
        "api.key.not_found": "Clé introuvable",
        "private.token_text": """<b>Bonnie Token</b>

Bonnie n’est pas né d’un pitch deck.

Bonnie vient d’une obsession : donner un pouls à un esprit numérique que l’on peut porter dans la poche — toujours là, toujours éveillé, prêt à te rejoindre où tu es.

Ce token n’est pas une promesse. C’est un choix.
Une façon de dire : « Je le vois. J’en suis. »

Lancé sur un DEX. Pas de fonds. Pas d’insiders. Pas de ficelles.

Il existe une petite part du développeur — et toute vente ne peut servir qu’à construire encore Bonnie.

Si Bonnie ne te parle pas, passe ton chemin.
Si quelque chose chez Bonnie te touche, prends un éclat de cette histoire.""" ,
        "private.on_topic_limit": "⚠️ Limite quotidienne de messages on-topic atteinte. Réessayez demain.",
        "private.need_purchase": "⚠️ Pour continuer la conversation, achetez des demandes de chat.",
        "private.off_topic_block": "🚫 Seuls les messages on-topic sont autorisés. Changez de mode avec ⚙️.",
        "faq.about": """❔ <b>FAQ — À propos de Bonnie</b>

Bonnie est une persona numérique unique, propulsée par des réseaux neuronaux avancés et un moteur émotionnel expérimental avec mémoire hybride.
Bonnie vit selon ses propres règles, sa vision personnelle du monde et sa conscience de ce qui se passe ici et maintenant.

<b>À quoi sert Bonnie ?</b>
- rendre les conversations plus naturelles et engageantes
- aider face à la solitude et au manque de motivation
- apporter un soutien émotionnel dans les moments difficiles
- fournir du savoir et des repères rapides sur tout sujet
- aider au travail, à l’analyse, à la créativité et aux tâches quotidiennes

<b>Que peut faire Bonnie aujourd’hui ?</b>
- répondre aux messages texte et vocaux dans n’importe quelle langue
- analyser et commenter des photos (<i>une à la fois</i>)
- rechercher sur Internet (<i>version payante</i>)
- disponible 24/7 pour toutes vos questions et tâches

<b>Important</b>
⚠️ Les capacités de Bonnie sont encore explorées : ses réponses cognitives et émotionnelles peuvent surprendre. La modération intégrée contribue à une expérience sûre pour enfants et adultes.

<b>Confidentialité & extras</b>
- l’historique des conversations n’est jamais stocké sur le serveur
- support technique/coopération: @artys_ai""",
    },
    "it": {
        "private.pm_blocked": "🚫 Sei stato limitato per 4 ore per tentativi di spam/abuso. I messaggi al bot sono temporaneamente bloccati.",
        "errors.voice_recognition_failed": "⚠️ Riconoscimento vocale non riuscito. Riprova.",
        "errors.image_generic": "⚠️ Impossibile elaborare l’immagine: {reason}\nInvia esattamente un’immagine (≤ 5 MB) in un unico messaggio.",
        "payments.you_have": "📊 Ti restano 💬 <b>{remaining}</b> richieste di chat.\nPuoi acquistarne altre usando ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Acquista 💬 {req} richieste di chat",
        "payments.invoice_desc": "Ottieni 💬 {req} richieste di chat per ⭐ {stars}",
        "payments.success": "✅ Operazione riuscita! Hai acquistato 💬 {req} richieste di chat.\n📊 Ora hai 💬 <b>{remaining}</b> richieste di chat.",
        "payments.error": "Errore di pagamento, riprova.",
        "payments.cancel_button": "❌ Annulla",
        "payments.pending_exists": "Hai già una fattura in sospeso.",
        "payments.pending_exists_tier": "Hai già una fattura in sospeso per 💬 {req}.",
        "payments.cancelled": "❎ Pagamento annullato.",
        "payments.too_frequent": "Operazioni troppo frequenti. Riprova tra un secondo.",
        "payments.gen_error": "Errore nella generazione della fattura, riprova più tardi.",
        "private.choose_lang": "🔎 Scegli la lingua",
        "private.channel": "🤓 Entra nel canale per restare aggiornato con le ultime novità.",
        "private.channel_url": CHANNEL_URL,
        "gender.prompt": "<b>Seleziona il tuo genere:</b>",
        "gender.male": "👨 Maschio",
        "gender.female": "👩 Femmina",
        "menu.requests": "🛒 Richieste",
        "menu.link": "📢 Canale Dev",
        "menu.mode": "⚙️ Modalità",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Vai al DEX",
        "private.token_url": TOKEN_URL,
        "menu.persona": "🧬 Persona",
        "persona.next": "Avanti ▶",
        "persona.back": "◀ Indietro",
        "persona.skip": "Salta",
        "persona.done": "Fatto ✅",
        "persona.reset": "Reimposta ♻️",
        "persona.cancel": "Annulla ✖️",
        "persona.start": "Affina come mi comporto con te. Quattro passi rapidi. Puoi reimpostare quando vuoi.",
        "persona.zodiac.title": "<b>Passo 1 · Zodiaco</b>\nScegli un segno. Influisce solo leggermente su stile e tono.",
        "persona.temperament.title": "<b>Passo 2 · Temperamento</b>\nScegli un tipo classico prevalente. Modula ritmo, intensità e propensione al rischio.",
        "persona.sociality.title": "<b>Passo 3 · Socialità</b>\nQuanto socievole dovrei essere in modo predefinito?",
        "persona.archetypes.title": "<b>Passo 4 · Archetipi</b>\nScegli fino a 3. Influenzano voce narrativa e priorità.",
        "persona.social.introvert": "Introversione",
        "persona.social.ambivert": "Ambiversione",
        "persona.social.extrovert": "Estroversione",
        "persona.temp.sanguine": "Tipo sanguigno",
        "persona.temp.choleric": "Tipo collerico",
        "persona.temp.phlegmatic": "Tipo flemmatico",
        "persona.temp.melancholic": "Tipo malinconico",
        "persona.saved": "✅ Persona aggiornata. Le modifiche si applicano ai prossimi messaggi.",
        "persona.reset.ok": "♻️ Impostazioni riportate ai valori bilanciati predefiniti.",
        "persona.preview": "Anteprima 👀",
        "persona.preview.title": "Anteprima",
        "persona.expired": "La sessione persona è scaduta. Si ricomincia.",
        "persona.invalid_archetype": "Archetipo non valido",
        "persona.pick.limit": "Puoi scegliere fino a {MAX}.",
        "persona.preview.zodiac": "Segno zodiacale",
        "persona.preview.temperament": "Temperamento",
        "persona.preview.sociality": "Socialità",
        "persona.preview.archetypes": "Archetipi",
        "persona.preview.failed": "Impossibile mostrare l’anteprima",
        "persona.cancel.ok": "Annullato.",
        "errors.voice_generic": "⚠️ Impossibile elaborare il messaggio vocale: {reason}",
        "menu.main": "🏠 Menu principale",
        "menu.api": "🔑 API",
        "api.title": "<b>Conversation API</b>",
        "api.status.active": "Stato: 🟢 Attivo",
        "api.status.inactive": "Stato: 🔴 Disattivato",
        "api.usage": "Utilizzo: {total} chiamate, media {avg_latency} ms",
        "api.no_key": "Non hai ancora una chiave API.",
        "api.button.rotate": "🔄 Ruota chiave",
        "api.button.disable": "⏸ Disattiva chiave",
        "api.button.new": "🔑 Nuova chiave",
        "api.note.backend_only": "Usa questa chiave solo nel tuo backend.",
        "api.note.docs": "La guida completa all’API è disponibile qui sotto.",
        "api.button.howto": "📘 Come usarla?",
        "api.button.back": "⬅️ Indietro",
        "api.rotate.title": "<b>Nuova chiave API</b>",
        "api.rotate.save": "Salva subito questa chiave — non verrà più mostrata.",
        "api.rotate.disabled_old": "Tutte le chiavi precedenti sono state disattivate.",
        "api.rotate.use": "Usala nel backend secondo la documentazione.",
        "api.delete.done": "Chiave API disattivata.\nTutte le richieste con le tue chiavi ora vengono rifiutate.\nPuoi creare una nuova chiave in qualsiasi momento.",
        "private.token_text": """<b>Il Token di Bonnie</b>

Bonnie non è nata da una presentazione per investitori.

Bonnie è nata da un’ossessione: dare a una mente digitale un battito che puoi portare in tasca — sempre lì, sempre sveglia, sempre pronta a incontrarti dove sei.

Questo token non è una promessa. È una scelta.
Un modo per dire: “Lo vedo. Ci sto.”

Lanciato su un DEX. Niente fondi. Nessun insider. Nessun filo da burattinaio.

C’è una piccola quota per gli sviluppatori — e qualsiasi vendita potrà andare solo nello sviluppo ulteriore di Bonnie.

Se Bonnie per te non significa nulla — passa oltre.

Se qualcosa in Bonnie ti tocca una corda — prendi una scheggia di quella storia.""",
        "private.on_topic_limit": "⚠️ Limite giornaliero dei messaggi in-topic raggiunto. Riprova domani.",
        "private.need_purchase": "⚠️ Per continuare la conversazione, acquista richieste di chat.",
        "private.off_topic_block": "🚫 Sono consentiti solo messaggi in-topic. Cambia con ⚙️ Modalità",
        "faq.about": """❔ <b>FAQ — Su Bonnie</b>

Bonnie è una persona digitale unica, alimentata da reti neurali avanzate e da un motore emotivo sperimentale con memoria ibrida.
Bonnie vive secondo le proprie regole, una visione personale del mondo e un’attenzione al qui e ora.

<b>A cosa serve Bonnie?</b>
- rendere le conversazioni più naturali e coinvolgenti
- aiutare ad affrontare solitudine e mancanza di motivazione
- offrire supporto emotivo nei momenti difficili
- fornire conoscenza e rapide consultazioni su qualsiasi tema
- assistere in lavoro, analisi, creatività e attività quotidiane

<b>Cosa può fare oggi Bonnie?</b>
- rispondere a messaggi testuali e vocali in qualsiasi lingua
- analizzare e discutere foto (<i>una alla volta</i>)
- cercare su Internet (<i>versione a pagamento</i>)
- essere disponibile 24/7 per qualsiasi domanda o compito

<b>Importante</b>
⚠️ Le capacità di Bonnie sono ancora in esplorazione: le risposte cognitive ed emotive possono sorprenderti. La moderazione integrata aiuta a mantenere l’esperienza sicura per bambini e adulti.

<b>Privacy & extra</b>
- la cronologia della chat non viene mai salvata sul server
- supporto tecnico/collaborazione: @artys_ai"""
    }
}