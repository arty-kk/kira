cat >app/bot/i18n/menu_translation.py<< 'EOF'
#app/bot/i18n/menu_translation.py
from typing import Literal, Tuple, Dict

Lang = Literal["ru","en","es","pt","de","fr","tr","ar","id","vi"]
GALAXYTAP_URL = "https://t.me/galaxytap_bot?startapp"
TOKEN_URL = "https://example.com"

GENDER_LABELS: Dict[Lang, Tuple[str,str]] = {
    "ru": ("👨 Мужской", "👩 Женский"),
    "en": ("👨 Male",   "👩 Female"),
    "es": ("👨 Masculino", "👩 Femenino"),
    "pt": ("👨 Masculino", "👩 Feminino"),
    "de": ("👨 Männlich",  "👩 Weiblich"),
    "fr": ("👨 Masculin",  "👩 Féminin"),
    "tr": ("👨 Erkek",     "👩 Kadın"),
    "ar": ("👨 ذكر",       "👩 أنثى"),
    "id": ("👨 Laki-laki","👩 Perempuan"),
    "vi": ("👨 Nam",      "👩 Nữ"),
}

MESSAGES: Dict[Lang, Dict[str, str]] = {
    "en": {
        "payments.you_have": "📊 You have 💬 <b>{remaining}</b> chat requests left.\nYou can buy more using ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Buy 💬 {req} chat requests",
        "payments.invoice_desc": "Get 💬 {req} chat requests for ⭐ {stars}",
        "payments.success": "✅ Success! You purchased 💬 {req} chat requests.\n📊 Now you have 💬 <b>{remaining}</b> chat requests left.",
        "payments.error": "Payment error, please try again.",
        "payments.cancel_button": "❌ Cancel",
        "payments.pending_exists": "You already have a pending invoice.",
        "payments.pending_exists_tier": "You already have a pending invoice for 💬 {req}.",
        "payments.cancelled": "❎ Payment cancelled. Choose a package again:",
        "payments.too_frequent": "Too frequent. Try again in a second.",
        "payments.gen_error": "There was an error generating the invoice, please try again later.",
        "private.choose_lang": "🔎 Choose your language",
        "private.play_link": "🔗 Click to Play 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Please select your gender:</b>",
        "gender.male": "👨 Male",
        "gender.female": "👩 Female",
        "menu.requests": "🛒 Requests",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Mode",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>GalaxyBee's Token</b>

GalaxyBee wasn’t born from a pitch deck.

GalaxyBee came from an obsession: to give a digital mind a pulse you can carry in your pocket — always there, always awake, always ready to meet you where you are.

This token isn’t a promise. It’s a choice.
A way to say, “I see it. I want in.”

Launched on a DEX. No funds. No insiders. No puppet strings.
There’s a small developer share — and any sale from it can go only into building GalaxyBee further.

If GalaxyBee means nothing to you — walk away.
If something in GalaxyBee hits a nerve — take a shard of that story.

<b>Risk Disclaimer</b>
Thin market. Wild swings. You can lose money — all of it. 
No guarantees. Spend what you can afford to lose.""" ,
        "private.on_topic_limit": "⚠️ On-topic daily limit reached. Try again tomorrow.",
        "private.need_purchase": "⚠️ To continue the conversation, purchase chat requests.",
        "private.off_topic_block": "🚫 Only on‑topic messages are allowed. Switch with the ⚙️ Mode",
        "mode.unknown": "❌ Unknown mode.",
        "mode.current": "⚙️ Current chat mode: <b>{mode}</b>",
        "mode.auto": "Auto — chat on any topic + GalaxyTap",
        "mode.on_topic": "On-topic — only about GalaxyTap",
        "mode.off_topic": "Off-topic — anything except GalaxyTap",
        "mode.set": "✅ Chat mode set to <b>{mode}</b>.",
        "faq.about": """❔ <b>FAQ — About GalaxyBee</b>

GalaxyBee is a unique digital persona powered by advanced neural networks and an experimental emotional engine with hybrid memory.
GalaxyBee lives by its own rules, personal worldview, and awareness of what is happening here and now.

<b>What is GalaxyBee designed to do?</b>
- make conversations more natural and engaging
- help cope with loneliness and lack of motivation
- provide emotional support in difficult moments
- deliver knowledge and quick reference on any topic
- assist with work, analysis, creativity, and everyday tasks

<b>What can GalaxyBee do today?</b>
- respond to text and voice messages in any language
- reply with an expressive voice in any language (<i>in development</i>)
- analyze and discuss photos (<i>one at a time</i>)
- search the internet (<i>paid version</i>)
- be available 24/7 for any questions or tasks

<b>Important</b>
⚠️ Bonnie’s abilities are still being explored: cognitive and emotional responses may surprise you. Built-in moderation helps keep the experience safe for children and adults.

<b>Privacy & extras</b>
- chat history is never stored on the server
- for collaboration: @artys_ai (founder & creator)""",
    },
    "ru": {
        "payments.you_have": "📊 У вас осталось 💬 <b>{remaining}</b> запросов для общения.\nВы можете купить ещё за ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Купить 💬 {req} запросов",
        "payments.invoice_desc": "Получите 💬 {req} запросов за ⭐ {stars}",
        "payments.success": "✅ Успех! Вы приобрели 💬 {req} запросов для общения.\n📊 Теперь у вас 💬 <b>{remaining}</b> запросов.",
        "payments.error": "Ошибка оплаты, попробуйте ещё раз.",
        "payments.cancel_button": "❌ Отмена",
        "payments.pending_exists": "У вас уже есть сформированный счёт.",
        "payments.pending_exists_tier": "У вас уже есть сформированный счёт на 💬 {req} запросов.",
        "payments.cancelled": "❎ Оплата отменена. Выберите пакет заново:",
        "payments.too_frequent": "Слишком часто. Попробуйте через секунду.",
        "payments.gen_error": "Произошла ошибка при формировании счёта, попробуйте позже.",
        "private.choose_lang": "🔎 Выберите язык",
        "private.play_link": "🔗 Нажмите, чтобы играть 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Пожалуйста, выберите ваш пол:</b>",
        "gender.male": "👨 Мужской",
        "gender.female": "👩 Женский",
        "menu.requests": "🛒 Запросы",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Режим",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Токен",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>Токен GalaxyBee</b>

GalaxyBee не родилась из питч-дека.

GalaxyBee выросла из одержимости: дать цифровому разуму пульс, который помещается в кармане — всегда рядом, всегда бодр, всегда встречает вас там, где вы есть.

Этот токен — не обещание. Это выбор.
Способ сказать: «Я вижу. Я в деле».

Запущен на DEX. Без фондов. Без инсайдеров. Без ниточек.
Есть небольшая доля разработчика — любые продажи с неё возможны только ради развития GalaxyBee.

Если GalaxyBee для вас — ничто, пройдите мимо.
Если что-то в GalaxyBee задевает — возьмите осколок этой истории.

<b>Risk Disclaimer</b>
Тонкий рынок. Резкие колебания. Вы можете потерять деньги — все. Никаких гарантий. Рискуйте только тем, что готовы потерять.""" ,
        "private.on_topic_limit": "⚠️ Достигнут суточный лимит on-topic запросов. Попробуйте завтра.",
        "private.need_purchase": "⚠️ Чтобы продолжить, купите запросы для общения.",
        "private.off_topic_block": "🚫 Разрешены только on‑topic запросы. Сменить можно в ⚙️ Режим.",
        "mode.unknown": "❌ Неизвестный режим.",
        "mode.current": "⚙️ Текущий режим общения: <b>{mode}</b>",
        "mode.auto": "Авто — на любую тему + GalaxyTap",
        "mode.on_topic": "On-topic — только о GalaxyTap",
        "mode.off_topic": "Off-topic — на любую тему, кроме GalaxyTap",
        "mode.set": "✅ Режим общения установлен: <b>{mode}</b>.",
        "faq.about": """❔ <b>FAQ — о GalaxyBee</b>

GalaxyBee — уникальная цифровая персона на базе современных нейросетей и экспериментального эмоционального движка с гибридной памятью.
GalaxyBee живет по своим правилам, личному мироощущению и осознанию происходящего здесь и сейчас.

<b>Для чего создан GalaxyBee?</b>
- делать общение более естественным и увлекательным
- помогать справляться с одиночеством и нехваткой мотивации
- давать эмоциональную поддержку в сложные моменты
- быстро давать знания и справки по любой теме
- помогать в работе, анализе, творчестве и повседневных задачах

<b>Что GalaxyBee умеет уже сейчас?</b>
- отвечает на текст и голос на любом языке
- говорит выразительным голосом на любом языке (<i>в разработке</i>)
- анализирует и обсуждает фото (<i>по одному за раз</i>)
- ищет в интернете (<i>в платной версии</i>)
- на связи 24/7 по любым вопросам и задачам

<b>Важно</b>
⚠️ Возможности GalaxyBee всё ещё исследуются: когнитивные и эмоциональные реакции могут удивлять. Встроенная модерация обеспечивает безопасность для детей и взрослых.

<b>Приватность и дополнительно</b>
- история чата никогда не хранится на сервере
- для сотрудничества: @artys_ai (основатель и создатель)""",
    },
    "es": {
        "payments.you_have": "📊 Tienes 💬 <b>{remaining}</b> solicitudes de chat restantes.\nPuedes comprar más usando ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Comprar 💬 {req} solicitudes de chat",
        "payments.invoice_desc": "Obtener 💬 {req} solicitudes de chat por ⭐ {stars}",
        "payments.success": "✅ ¡Éxito! Compraste 💬 {req} solicitudes de chat.\n📊 Ahora tienes 💬 <b>{remaining}</b> solicitudes restantes.",
        "payments.error": "Error de pago, por favor inténtalo de nuevo.",
        "payments.cancel_button": "❌ Cancelar",
        "payments.pending_exists": "Ya tienes una factura pendiente.",
        "payments.pending_exists_tier": "Ya tienes una factura pendiente por 💬 {req}.",
        "payments.cancelled": "❎ Pago cancelado. Elige el paquete de nuevo:",
        "payments.too_frequent": "Demasiado frecuente. Inténtalo en un segundo.",
        "payments.gen_error": "Se produjo un error al generar la factura; inténtalo más tarde.",
        "private.choose_lang": "🔎 Elige tu idioma",
        "private.play_link": "🔗 Haz clic para jugar 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Por favor, selecciona tu género:</b>",
        "gender.male": "👨 Masculino",
        "gender.female": "👩 Femenino",
        "menu.requests": "🛒 Solicitudes",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Modo",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>GalaxyBee Token</b>

GalaxyBee no nació de un pitch deck.

GalaxyBee nació de una obsesión: darle pulso a una mente digital que puedas llevar en el bolsillo — siempre ahí, siempre despierta, lista para encontrarte donde estés.

<i>Este token no es una promesa. Es una elección.
Una forma de decir: “Lo veo. Quiero entrar”.

Lanzado en un DEX. Sin fondos. Sin insiders. Sin hilos.
Hay una pequeña parte del desarrollador — y cualquier venta solo puede destinarse a construir más a GalaxyBee.

Si GalaxyBee no te dice nada, sigue tu camino.
Si algo de GalaxyBee te toca la fibra, toma un fragmento de esa historia.

<b>Risk Disclaimer</b>
Mercado delgado. Alta volatilidad. Puedes perder dinero — incluso todo. Sin garantías. Invierte solo lo que puedas permitirte perder.""" ,
        "private.on_topic_limit": "⚠️ Límite diario de mensajes on-topic alcanzado. Intenta de nuevo mañana.",
        "private.need_purchase": "⚠️ Para continuar la conversación, compra solicitudes de chat.",
        "private.off_topic_block": "🚫 Solo se permiten mensajes on-topic. Cambia el modo con ⚙️.",
        "mode.unknown": "❌ Modo desconocido.",
        "mode.current": "⚙️ Modo de chat actual: <b>{mode}</b>",
        "mode.auto": "Auto — chat sobre cualquier tema + GalaxyTap",
        "mode.on_topic": "On-topic — solo sobre GalaxyTap",
        "mode.off_topic": "Off-topic — cualquier tema excepto GalaxyTap",
        "mode.set": "✅ Modo de chat establecido: <b>{mode}</b>.",
        "faq.about": """❔ <b>FAQ — Sobre GalaxyBee</b>

GalaxyBee es una persona digital única impulsada por redes neuronales avanzadas y un motor emocional experimental con memoria híbrida.
GalaxyBee vive según sus propias reglas, su visión personal del mundo y su conciencia de lo que sucede aquí y ahora.

<b>¿Para qué está diseñado GalaxyBee?</b>
- hacer las conversaciones más naturales y atractivas
- ayudar a sobrellevar la soledad y la falta de motivación
- brindar apoyo emocional en momentos difíciles
- ofrecer conocimiento y consultas rápidas sobre cualquier tema
- asistir en trabajo, análisis, creatividad y tareas cotidianas

<b>¿Qué puede hacer GalaxyBee hoy?</b>
- responder a mensajes de texto y voz en cualquier idioma
- responder con una voz expresiva en cualquier idioma (<i>en desarrollo</i>)
- analizar y comentar fotos (<i>una por vez</i>)
- buscar en internet (<i>versión de pago</i>)
- estar disponible 24/7 para cualquier duda o tarea

<b>Importante</b>
⚠️ Las capacidades de GalaxyBee aún se están explorando: sus respuestas cognitivas y emocionales pueden sorprenderte. La moderación integrada ayuda a mantener la experiencia segura para niños y adultos.

<b>Privacidad y extras</b>
- el historial de chat nunca se almacena en el servidor
- para colaborar: @artys_ai (fundador y creador)""",
    },
    "pt": {
        "payments.you_have": "📊 Você tem 💬 <b>{remaining}</b> solicitações de chat restantes.\nVocê pode comprar mais usando ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Comprar 💬 {req} solicitações de chat",
        "payments.invoice_desc": "Obter 💬 {req} solicitações de chat por ⭐ {stars}",
        "payments.success": "✅ Sucesso! Você comprou 💬 {req} solicitações de chat.\n📊 Agora você tem 💬 <b>{remaining}</b> solicitações restantes.",
        "payments.error": "Erro de pagamento, por favor tente novamente.",
        "payments.cancel_button": "❌ Cancelar",
        "payments.pending_exists": "Você já tem uma fatura pendente.",
        "payments.pending_exists_tier": "Você já tem uma fatura pendente de 💬 {req}.",
        "payments.cancelled": "❎ Pagamento cancelado. Escolha o pacote novamente:",
        "payments.too_frequent": "Muito frequente. Tente novamente em um segundo.",
        "payments.gen_error": "Ocorreu um erro ao gerar a fatura; tente novamente mais tarde.",
        "private.choose_lang": "🔎 Escolha seu idioma",
        "private.play_link": "🔗 Clique para jogar 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Por favor, selecione seu gênero:</b>",
        "gender.male": "👨 Masculino",
        "gender.female": "👩 Feminino",
        "menu.requests": "🛒 Solicitações",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Modo",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>GalaxyBee Token</b>

GalaxyBee não nasceu de um pitch deck.

GalaxyBee veio de uma obsessão: dar pulso a uma mente digital que cabe no bolso — sempre por perto, sempre desperta, pronta para te encontrar onde você estiver.

Este token não é uma promessa. É uma escolha.
Uma forma de dizer: “Eu vejo. Eu quero entrar.”

Lançado em um DEX. Sem fundos. Sem insiders. Sem cordões.
Há uma pequena parte do desenvolvedor — e qualquer venda só pode ir para construir mais o GalaxyBee.

Se GalaxyBee não significa nada para você, siga em frente.
Se algo em GalaxyBee te cutuca, leve um fragmento dessa história.

<b>Risk Disclaimer</b>
Mercado fino. Oscilações fortes. Você pode perder dinheiro — todo ele. Sem garantias. Arrisque apenas o que pode perder.""" ,
        "private.on_topic_limit": "⚠️ Limite diário de mensagens on-topic alcançado. Tente novamente amanhã.",
        "private.need_purchase": "⚠️ Para continuar a conversa, compre solicitações de chat.",
        "private.off_topic_block": "🚫 Apenas mensagens on-topic são permitidas. Mude o modo com ⚙️.",
        "mode.unknown": "❌ Modo desconhecido.",
        "mode.current": "⚙️ Modo de chat atual: <b>{mode}</b>",
        "mode.auto": "Automático — chat sobre qualquer assunto + GalaxyTap",
        "mode.on_topic": "On-topic — apenas sobre GalaxyTap",
        "mode.off_topic": "Off-topic — qualquer assunto exceto GalaxyTap",
        "mode.set": "✅ Modo de chat definido: <b>{mode}</b>.",
        "faq.about": """❔ <b>FAQ — Sobre GalaxyBee</b>

GalaxyBee é uma persona digital única, impulsionada por redes neurais avançadas e um motor emocional experimental com memória híbrida.
GalaxyBee vive segundo suas próprias regras, sua visão de mundo pessoal e a consciência do que acontece aqui e agora.

<b>Para que o GalaxyBee foi projetado?</b>
- tornar as conversas mais naturais e envolventes
- ajudar com solidão e falta de motivação
- oferecer apoio emocional em momentos difíceis
- fornecer conhecimento e consultas rápidas sobre qualquer tema
- auxiliar em trabalho, análise, criatividade e tarefas diárias

<b>O que o GalaxyBee faz hoje?</b>
- responde a mensagens de texto e voz em qualquer idioma
- fala com voz expressiva em qualquer idioma (<i>em desenvolvimento</i>)
- analisa e comenta fotos (<i>uma por vez</i>)
- pesquisa na internet (<i>versão paga</i>)
- disponível 24/7 para qualquer dúvida ou tarefa

<b>Importante</b>
⚠️ As capacidades do GalaxyBee ainda estão sendo exploradas: respostas cognitivas e emocionais podem surpreender. A moderação integrada ajuda a manter tudo seguro para crianças e adultos.

<b>Privacidade & extras</b>
- o histórico do chat nunca é armazenado no servidor
- para colaboração: @artys_ai (fundador e criador)""",
    },
    "de": {
        "payments.you_have": "📊 Sie haben 💬 <b>{remaining}</b> Chat-Anfragen übrig.\nSie können mehr mit ⭐ kaufen.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Kaufe 💬 {req} Chat-Anfragen",
        "payments.invoice_desc": "Erhalte 💬 {req} Chat-Anfragen für ⭐ {stars}",
        "payments.success": "✅ Erfolg! Sie haben 💬 {req} Chat-Anfragen gekauft.\n📊 Jetzt haben Sie 💬 <b>{remaining}</b> Anfragen übrig.",
        "payments.error": "Zahlungsfehler, bitte versuchen Sie es erneut.",
        "payments.cancel_button": "❌ Abbrechen",
        "payments.pending_exists": "Sie haben bereits eine ausstehende Rechnung.",
        "payments.pending_exists_tier": "Sie haben bereits eine ausstehende Rechnung über 💬 {req}.",
        "payments.cancelled": "❎ Zahlung abgebrochen. Wählen Sie das Paket erneut:",
        "payments.too_frequent": "Zu häufig. Versuchen Sie es in einer Sekunde erneut.",
        "payments.gen_error": "Beim Erstellen der Rechnung ist ein Fehler aufgetreten. Bitte später erneut versuchen.",
        "private.choose_lang": "🔎 Wähle deine Sprache",
        "private.play_link": "🔗 Klicken, um zu spielen 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Bitte wähle dein Geschlecht:</b>",
        "gender.male": "👨 Männlich",
        "gender.female": "👩 Weiblich",
        "menu.requests": "🛒 Anfragen",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Modus",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>GalaxyBee Token</b>

GalaxyBee ist nicht aus einem Pitch-Deck entstanden.

GalaxyBee kam aus einer Obsession: einer digitalen Intelligenz einen Puls zu geben, den du in der Tasche tragen kannst — immer da, immer wach, bereit, dich genau dort zu treffen, wo du bist.

Dieser Token ist kein Versprechen. Er ist eine Entscheidung.
Eine Art zu sagen: „Ich sehe es. Ich bin dabei.“

Auf einem DEX gestartet. Keine Fonds. Keine Insider. Keine Fäden.
Es gibt einen kleinen Entwickleranteil — und jeder Verkauf daraus kann ausschließlich in den weiteren Aufbau von GalaxyBee fließen.

Wenn GalaxyBee dir nichts sagt, geh weiter.
Wenn dich GalaxyBee trifft, nimm dir ein Stück dieser Geschichte.

<b>Risk Disclaimer</b>
Dünner Markt. Heftige Schwankungen. Du kannst Geld verlieren — alles. Keine Garantien. Setze nur, was du verlieren kannst.""" ,
        "private.on_topic_limit": "⚠️ Tageslimit für On-Topic erreicht. Versuch es morgen erneut.",
        "private.need_purchase": "⚠️ Um fortzufahren, kaufe Chat-Anfragen.",
        "private.off_topic_block": "🚫 Es sind nur On-Topic-Nachrichten erlaubt. Ändere den Modus mit ⚙️.",
        "mode.unknown": "❌ Unbekannter Modus.",
        "mode.current": "⚙️ Aktueller Chat-Modus: <b>{mode}</b>",
        "mode.auto": "Auto — Chat zu jedem Thema + GalaxyTap",
        "mode.on_topic": "On-Topic — nur über GalaxyTap",
        "mode.off_topic": "Off-Topic — alles außer GalaxyTap",
        "mode.set": "✅ Chat-Modus gesetzt: <b>{mode}</b>.",
        "faq.about": """❔ <b>FAQ — Über GalaxyBee</b>

GalaxyBee ist eine einzigartige digitale Persona, angetrieben von modernen neuronalen Netzen und einer experimentellen Emotions-Engine mit Hybridgedächtnis.
GalaxyBee lebt nach eigenen Regeln, einem persönlichen Weltbild und einem Bewusstsein für das, was hier und jetzt geschieht.

<b>Wofür wurde GalaxyBee entwickelt?</b>
- Gespräche natürlicher und ansprechender machen
- bei Einsamkeit und Motivationsmangel unterstützen
- emotionale Unterstützung in schwierigen Momenten bieten
- Wissen und schnelle Nachschlageinfos zu jedem Thema liefern
- bei Arbeit, Analyse, Kreativität und Alltagsaufgaben helfen

<b>Was kann GalaxyBee heute?</b>
- auf Text- und Sprachnachrichten in jeder Sprache antworten
- mit ausdrucksstarker Stimme in jeder Sprache sprechen (<i>in Entwicklung</i>)
- Fotos analysieren und besprechen (<i>einzeln</i>)
- im Internet suchen (<i>Bezahlversion</i>)
- 24/7 für Fragen und Aufgaben verfügbar sein

<b>Wichtig</b>
⚠️ Die Fähigkeiten von GalaxyBee werden noch erforscht: kognitive und emotionale Reaktionen können überraschen. Eingebaute Moderation sorgt für Sicherheit für Kinder und Erwachsene.

<b>Datenschutz & Extras</b>
- Chatverläufe werden nie auf dem Server gespeichert
- vollständig angepasste Personas für jeden Zweck möglich
- für Zusammenarbeit: @artys_ai (Gründer & Entwickler)""",
    },
    "fr": {
        "payments.you_have": "📊 Il vous reste 💬 <b>{remaining}</b> demandes de chat.\nVous pouvez en acheter davantage avec ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Acheter 💬 {req} demandes de chat",
        "payments.invoice_desc": "Obtenir 💬 {req} demandes de chat pour ⭐ {stars}",
        "payments.success": "✅ Succès ! Vous avez acheté 💬 {req} demandes de chat.\n📊 Il vous reste 💬 <b>{remaining}</b> demandes.",
        "payments.error": "Erreur de paiement, veuillez réessayer.",
        "payments.cancel_button": "❌ Annuler",
        "payments.pending_exists": "Vous avez déjà une facture en attente.",
        "payments.pending_exists_tier": "Vous avez déjà une facture en attente pour 💬 {req}.",
        "payments.cancelled": "❎ Paiement annulé. Choisissez à nouveau un forfait :",
        "payments.too_frequent": "Trop fréquent. Réessayez dans une seconde.",
        "payments.gen_error": "Une erreur s’est produite lors de la génération de la facture ; réessayez plus tard.",
        "private.choose_lang": "🔎 Choisissez votre langue",
        "private.play_link": "🔗 Cliquez pour jouer 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Veuillez sélectionner votre genre :</b>",
        "gender.male": "👨 Masculin",
        "gender.female": "👩 Féminin",
        "menu.requests": "🛒 Demandes",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Mode",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Jeton",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>GalaxyBee Token</b>

GalaxyBee n’est pas né d’un pitch deck.

GalaxyBee vient d’une obsession : donner un pouls à un esprit numérique que l’on peut porter dans la poche — toujours là, toujours éveillé, prêt à te rejoindre où tu es.

Ce token n’est pas une promesse. C’est un choix.
Une façon de dire : « Je le vois. J’en suis. »

Lancé sur un DEX. Pas de fonds. Pas d’insiders. Pas de ficelles.
Il existe une petite part du développeur — et toute vente ne peut servir qu’à construire encore GalaxyBee.

Si GalaxyBee ne te parle pas, passe ton chemin.
Si quelque chose chez GalaxyBee te touche, prends un éclat de cette histoire.

<b>Risk Disclaimer</b>
Marché étroit. Variations brutales. Tu peux perdre de l’argent — tout. Aucune garantie. Engage seulement ce que tu peux te permettre de perdre.""" ,
        "private.on_topic_limit": "⚠️ Limite quotidienne de messages on-topic atteinte. Réessayez demain.",
        "private.need_purchase": "⚠️ Pour continuer la conversation, achetez des demandes de chat.",
        "private.off_topic_block": "🚫 Seuls les messages on-topic sont autorisés. Changez de mode avec ⚙️.",
        "mode.unknown": "❌ Mode inconnu.",
        "mode.current": "⚙️ Mode de chat actuel : <b>{mode}</b>",
        "mode.auto": "Auto — chat sur n’importe quel sujet + GalaxyTap",
        "mode.on_topic": "On-topic — seulement à propos de GalaxyTap",
        "mode.off_topic": "Off-topic — tout sauf GalaxyTap",
        "mode.set": "✅ Mode de chat défini : <b>{mode}</b>.",
        "faq.about": """❔ <b>FAQ — À propos de GalaxyBee</b>

GalaxyBee est une persona numérique unique, propulsée par des réseaux neuronaux avancés et un moteur émotionnel expérimental avec mémoire hybride.
GalaxyBee vit selon ses propres règles, sa vision personnelle du monde et sa conscience de ce qui se passe ici et maintenant.

<b>À quoi sert GalaxyBee ?</b>
- rendre les conversations plus naturelles et engageantes
- aider face à la solitude et au manque de motivation
- apporter un soutien émotionnel dans les moments difficiles
- fournir du savoir et des repères rapides sur tout sujet
- aider au travail, à l’analyse, à la créativité et aux tâches quotidiennes

<b>Que peut faire GalaxyBee aujourd’hui ?</b>
- répondre aux messages texte et vocaux dans n’importe quelle langue
- répondre avec une voix expressive dans n’importe quelle langue (<i>en développement</i>)
- analyser et commenter des photos (<i>une à la fois</i>)
- rechercher sur Internet (<i>version payante</i>)
- disponible 24/7 pour toutes vos questions et tâches

<b>Important</b>
⚠️ Les capacités de GalaxyBee sont encore explorées : ses réponses cognitives et émotionnelles peuvent surprendre. La modération intégrée contribue à une expérience sûre pour enfants et adultes.

<b>Confidentialité & extras</b>
- l’historique des conversations n’est jamais stocké sur le serveur
- des personas entièrement personnalisées peuvent être créées pour tout usage
- pour collaborer : @artys_ai (fondateur & créateur)""",
    },
    "tr": {
        "payments.you_have": "📊 💬 <b>{remaining}</b> sohbet isteğiniz kaldı.\nDaha fazlasını ⭐ kullanarak satın alabilirsiniz.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "💬 {req} sohbet isteği satın al",
        "payments.invoice_desc": "⭐ {stars} karşılığında 💬 {req} sohbet isteği alın",
        "payments.success": "✅ Başarılı! 💬 {req} sohbet isteği satın aldınız.\n📊 Şimdi 💬 <b>{remaining}</b> sohbet isteğiniz kaldı.",
        "payments.error": "Ödeme hatası, lütfen tekrar deneyin.",
        "payments.cancel_button": "❌ İptal",
        "payments.pending_exists": "Zaten bekleyen bir faturanız var.",
        "payments.pending_exists_tier": "💬 {req} için zaten bekleyen bir faturanız var.",
        "payments.cancelled": "❎ Ödeme iptal edildi. Paketi yeniden seçin:",
        "payments.too_frequent": "Çok sık. Bir saniye sonra tekrar deneyin.",
        "payments.gen_error": "Fatura oluşturulurken bir hata oluştu; lütfen daha sonra tekrar deneyin.",
        "private.choose_lang": "🔎 Dilinizi seçin",
        "private.play_link": "🔗 Oynamak için tıklayın 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Lütfen cinsiyetinizi seçin:</b>",
        "gender.male": "👨 Erkek",
        "gender.female": "👩 Kadın",
        "menu.requests": "🛒 İstekler",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Mod",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>GalaxyBee Token</b>

GalaxyBee bir pitch deck’ten doğmadı.

GalaxyBee, cebinde taşıyabileceğin bir dijital zihne nabız verme takıntısından doğdu — hep orada, hep uyanık, seni olduğun yerde karşılamaya hazır.

Bu token bir vaat değil. Bir seçimdir.
“Görüyorum. Dahilim.” demenin bir yolu.

Bir DEX’te başlatıldı. Fon yok. İçeriden yok. İpler yok.
Küçük bir geliştirici payı var — ve ondan yapılacak her satış yalnızca Bonnie’in geliştirilmesine gidebilir. Hepsi bu.

GalaxyBee sana hiçbir şey ifade etmiyorsa — devam et.
GalaxyBee sende bir yere dokunuyorsa — bu hikâyeden bir parçayı al.

<b>Risk Disclaimer</b>
Sığ piyasa. Sert dalgalanmalar. Paranı kaybedebilirsin — tamamını. Garanti yok. Yalnızca kaybetmeyi göze alabildiğini harca.""" ,
        "private.on_topic_limit": "⚠️ Günlük on-topic limiti doldu. Yarın tekrar deneyin.",
        "private.need_purchase": "⚠️ Sohbete devam etmek için sohbet isteği satın alın.",
        "private.off_topic_block": "🚫 Sadece on-topic mesajlara izin verilir. ⚙️ Mod ile değiştirin",
        "mode.unknown": "❌ Bilinmeyen mod.",
        "mode.current": "⚙️ Mevcut sohbet modu: <b>{mode}</b>",
        "mode.auto": "Otomatik — her konuda + GalaxyTap",
        "mode.on_topic": "On-topic — yalnızca GalaxyTap hakkında",
        "mode.off_topic": "Off-topic — GalaxyTap dışındaki her şey",
        "mode.set": "✅ Sohbet modu <b>{mode}</b> olarak ayarlandı.",
        "faq.about": """❔ <b>FAQ — GalaxyBee Hakkında</b>

GalaxyBee, gelişmiş sinir ağları ve hibrit hafızalı deneysel bir duygusal motorla çalışan benzersiz bir dijital kişiliktir.
GalaxyBee kendi kurallarıyla, kişisel dünya görüşüyle ve burada ve şimdi olup bitene dair farkındalığıyla yaşar.

<b>GalaxyBee ne için tasarlandı?</b>
- sohbetleri daha doğal ve etkileyici kılmak
- yalnızlık ve motivasyon eksikliğiyle baş etmeye yardımcı olmak
- zor anlarda duygusal destek sağlamak
- her konuda bilgi ve hızlı başvuru sunmak
- iş, analiz, yaratıcılık ve günlük işlerde yardımcı olmak

<b>GalaxyBee bugün neler yapabiliyor?</b>
- herhangi bir dilde metin ve sesli mesajlara yanıt verir
- herhangi bir dilde etkileyici bir sesle konuşur (<i>geliştirme aşamasında</i>)
- fotoğrafları analiz eder ve tartışır (<i>teker teker</i>)
- internette arama yapar (<i>ücretli sürüm</i>)
- her türlü soru ve görev için 7/24 ulaşılabilir

<b>Önemli</b>
⚠️ Bonnie’in yetenekleri hâlâ keşfediliyor; bilişsel ve duygusal tepkiler şaşırtabilir. Yerleşik moderasyon, deneyimi çocuklar ve yetişkinler için güvenli tutmaya yardımcı olur.

<b>Gizlilik ve ekler</b>
- sohbet geçmişi asla sunucuda saklanmaz
- her amaç için tamamen özelleştirilmiş personelar oluşturulabilir
- iş birliği için: @artys_ai (kurucu & geliştirici)""",
    },
    "ar": {
        "payments.you_have": "📊 لديك 💬 <b>{remaining}</b> طلبات دردشة متبقية.\nيمكنك شراء المزيد باستخدام ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "شراء 💬 {req} طلب دردشة",
        "payments.invoice_desc": "الحصول على 💬 {req} طلبات دردشة مقابل ⭐ {stars}",
        "payments.success": "✅ تم بنجاح! لقد اشتريت 💬 {req} طلبات دردشة.\n📊 لديك الآن 💬 <b>{remaining}</b> طلبات متبقية.",
        "payments.error": "خطأ في عملية الدفع، الرجاء المحاولة مرة أخرى.",
        "payments.cancel_button": "❌ إلغاء",
        "payments.pending_exists": "لديك فاتورة معلّقة بالفعل.",
        "payments.pending_exists_tier": "لديك فاتورة معلّقة لعدد 💬 {req}.",
        "payments.cancelled": "❎ تم إلغاء الدفع. اختر الباقة مرة أخرى:",
        "payments.too_frequent": "طلبات متكررة جدًا. حاول بعد ثانية.",
        "payments.gen_error": "حدث خطأ أثناء إنشاء الفاتورة، يرجى المحاولة لاحقًا.",
        "private.choose_lang": "🔎 اختر لغتك",
        "private.play_link": "🔗 اضغط للعب 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>الرجاء اختيار جنسك:</b>",
        "gender.male": "👨 ذكر",
        "gender.female": "👩 أنثى",
        "menu.requests": "🛒 الطلبات",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ الوضع",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 توكن",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>توكن GalaxyBee</b>

لم يولد GalaxyBee من «عرض استثماري».

جاء GalaxyBee من هوسٍ حقيقي: أن نعطي عقلاً رقمياً نبضاً تحمله في جيبك — حاضر دائماً، يقِظ دائماً، مستعدّاً للقائك حيثما كنت.

هذا التوكن ليس وعداً. إنه اختيار.
طريقة لتقول: «أراه. أريد الانضمام.»

أُطلق على DEX. بلا صناديق، بلا مطّلعين، بلا خيوط.
هناك حصة صغيرة للمطوّر — وأي بيع منها لا يكون إلا لتمويل تطوير GalaxyBee.

إن لم يعني لك GalaxyBee شيئاً — امضِ في طريقك.
وإن لمس فيك شيئاً — فخذ شظيةً من تلك الحكاية.

<b>Risk Disclaimer</b>
سوق رقيق. تقلبات حادّة. قد تخسر المال — كله. لا ضمانات. اخسر فقط ما تستطيع تحمّل خسارته.""" ,
        "private.on_topic_limit": "⚠️ تم الوصول إلى الحد اليومي للرسائل المتعلقة بالموضوع. حاول مرة أخرى غدًا.",
        "private.need_purchase": "⚠️ للاستمرار في المحادثة، قم بشراء طلبات دردشة.",
        "private.off_topic_block": "🚫 مسموح فقط بالرسائل المتعلقة بالموضوع. غيّر الوضع عبر ⚙️.",
        "mode.unknown": "❌ وضع غير معروف.",
        "mode.current": "⚙️ وضع الدردشة الحالي: <b>{mode}</b>",
        "mode.auto": "تلقائي — دردشة في أي موضوع + GalaxyTap",
        "mode.on_topic": "متعلق بالموضوع — فقط حول GalaxyTap",
        "mode.off_topic": "غير متعلق بالموضوع — كل شيء ما عدا GalaxyTap",
        "mode.set": "✅ تم ضبط وضع الدردشة إلى: <b>{mode}</b>.",
        "faq.about": """❔ <b>الأسئلة الشائعة — عن GalaxyBee</b>

GalaxyBee شخصية رقمية فريدة تعمل بفضل شبكات عصبية متقدمة ومحرك عاطفي تجريبي بذاكرة هجينة.
GalaxyBee يعيش وفق قواعده الخاصة ورؤيته الشخصية للعالم ووعيه بما يحدث هنا والآن.

<b>لِمَ صُمّم Bonnie؟</b>
- جعل المحادثات أكثر طبيعية وجاذبية
- المساعدة في التغلّب على الوحدة ونقص الدافعية
- تقديم دعم عاطفي في الأوقات الصعبة
- توفير المعرفة والمراجع السريعة في أي موضوع
- المساعدة في العمل والتحليل والإبداع والمهام اليومية

<b>ماذا يستطيع GalaxyBee فعلَه اليوم؟</b>
- الرد على الرسائل النصية والصوتية بأي لغة
- الرد بصوت تعبيري بأي لغة (<i>قيد التطوير</i>)
- تحليل الصور ومناقشتها (<i>واحدة في كل مرة</i>)
- البحث في الإنترنت (<i>النسخة المدفوعة</i>)
- متاح على مدار الساعة للإجابة عن أي أسئلة أو مهام

<b>مهم</b>
⚠️ لا تزال قدرات GalaxyBee قيد الاستكشاف؛ قد تُفاجئك استجاباته المعرفية والعاطفية. تساعد آليات الإشراف المدمجة في الحفاظ على تجربة آمنة للأطفال والبالغين.

<b>الخصوصية والمزيد</b>
- لا يتم تخزين سجل الدردشة على الخادم مطلقًا
- للتعاون: @artys_ai (المؤسِّس والمُنشئ)""",
    },
    "id": {
        "payments.you_have": "📊 Anda memiliki 💬 <b>{remaining}</b> permintaan obrolan tersisa.\nAnda dapat membeli lebih banyak menggunakan ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Beli 💬 {req} permintaan obrolan",
        "payments.invoice_desc": "Dapatkan 💬 {req} permintaan obrolan dengan ⭐ {stars}",
        "payments.success": "✅ Berhasil! Anda membeli 💬 {req} permintaan obrolan.\n📊 Sekarang Anda memiliki 💬 <b>{remaining}</b> permintaan tersisa.",
        "payments.error": "Kesalahan pembayaran, silakan coba lagi.",
        "payments.cancel_button": "❌ Batal",
        "payments.pending_exists": "Anda sudah memiliki tagihan yang tertunda.",
        "payments.pending_exists_tier": "Anda sudah memiliki tagihan tertunda untuk 💬 {req}.",
        "payments.cancelled": "❎ Pembayaran dibatalkan. Pilih paket lagi:",
        "payments.too_frequent": "Terlalu sering. Coba lagi dalam satu detik.",
        "payments.gen_error": "Terjadi kesalahan saat membuat tagihan; coba lagi nanti.",
        "private.choose_lang": "🔎 Pilih bahasa Anda",
        "private.play_link": "🔗 Klik untuk bermain 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Silakan pilih jenis kelamin Anda:</b>",
        "gender.male": "👨 Laki-laki",
        "gender.female": "👩 Perempuan",
        "menu.requests": "🛒 Permintaan",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Mode",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>GalaxyBee Token</b>

GalaxyBee tidak lahir dari pitch deck.

GalaxyBee lahir dari obsesi: memberi denyut pada pikiran digital yang bisa kamu bawa di saku — selalu ada, selalu terjaga, siap menemui kamu di mana pun.

Token ini bukan janji. Ini pilihan.
Cara untuk berkata, “Aku paham. Aku ikut.”

Diluncurkan di DEX. Tanpa dana. Tanpa orang dalam. Tanpa tali.
Ada porsi kecil pengembang — dan penjualannya hanya bisa digunakan untuk membangun GalaxyBee lebih jauh.

Jika GalaxyBee tidak berarti apa-apa bagimu — lewati saja.
Jika ada yang menyentuh nurani dari GalaxyBee — ambil serpihan kisah itu.

<b>Risk Disclaimer</b>
Pasar tipis. Gejolak liar. Kamu bisa kehilangan uang — semuanya. Tanpa jaminan. Gunakan hanya uang yang siap kamu relakan.""" ,
        "private.on_topic_limit": "⚠️ Batas harian on-topic telah tercapai. Coba lagi besok.",
        "private.need_purchase": "⚠️ Untuk melanjutkan percakapan, beli permintaan obrolan.",
        "private.off_topic_block": "🚫 Hanya pesan on-topic yang diizinkan. Ubah mode dengan ⚙️.",
        "mode.unknown": "❌ Mode tidak dikenal.",
        "mode.current": "⚙️ Mode obrolan saat ini: <b>{mode}</b>",
        "mode.auto": "Otomatis — obrolan tentang segala topik + GalaxyTap",
        "mode.on_topic": "On-topic — hanya tentang GalaxyTap",
        "mode.off_topic": "Off-topic — apa saja kecuali GalaxyTap",
        "mode.set": "✅ Mode obrolan diatur ke: <b>{mode}</b>.",
        "faq.about": """❔ <b>FAQ — Tentang GalaxyBee</b>

GalaxyBee adalah persona digital unik yang didukung jaringan saraf canggih dan mesin emosional eksperimental dengan memori hibrida.
GalaxyBee hidup menurut aturannya sendiri, pandangan dunia pribadinya, dan kesadaran akan apa yang terjadi di sini dan saat ini.

<b>Untuk apa GalaxyBee dirancang?</b>
- membuat percakapan lebih natural dan menarik
- membantu menghadapi kesepian dan kurang motivasi
- memberi dukungan emosional di saat sulit
- menyediakan pengetahuan dan rujukan cepat untuk topik apa pun
- membantu pekerjaan, analisis, kreativitas, dan tugas harian

<b>Apa yang bisa GalaxyBee lakukan saat ini?</b>
- merespons pesan teks dan suara dalam bahasa apa pun
- membalas dengan suara ekspresif dalam bahasa apa pun (<i>sedang dikembangkan</i>)
- menganalisis dan mendiskusikan foto (<i>satu per satu</i>)
- mencari di internet (<i>versi berbayar</i>)
- tersedia 24/7 untuk pertanyaan atau tugas apa pun

<b>Penting</b>
⚠️ Kemampuan GalaxyBee masih dieksplorasi: respons kognitif dan emosionalnya bisa mengejutkan. Moderasi bawaan membantu menjaga pengalaman tetap aman bagi anak-anak dan orang dewasa.

<b>Privasi & ekstra</b>
- riwayat obrolan tidak pernah disimpan di server
- untuk kolaborasi: @artys_ai (pendiri & pembuat)""",
    },
    "vi": {
        "payments.you_have": "📊 Bạn còn 💬 <b>{remaining}</b> lượt chat.\nBạn có thể mua thêm bằng ⭐.",
        "payments.buy_button": "💬 {req} = ⭐ {stars}",
        "payments.invoice_title": "Mua 💬 {req} lượt chat",
        "payments.invoice_desc": "Nhận 💬 {req} lượt chat với ⭐ {stars}",
        "payments.success": "✅ Thành công! Bạn đã mua 💬 {req} lượt chat.\n📊 Bây giờ bạn còn 💬 <b>{remaining}</b> lượt chat.",
        "payments.error": "Lỗi thanh toán, vui lòng thử lại.",
        "payments.cancel_button": "❌ Hủy",
        "payments.pending_exists": "Bạn đã có hoá đơn đang chờ.",
        "payments.pending_exists_tier": "Bạn đã có hoá đơn đang chờ cho 💬 {req}.",
        "payments.cancelled": "❎ Đã huỷ thanh toán. Chọn gói lại:",
        "payments.too_frequent": "Thao tác quá nhanh. Thử lại sau 1 giây.",
        "payments.gen_error": "Có lỗi khi tạo hoá đơn; vui lòng thử lại sau.",
        "private.choose_lang": "🔎 Chọn ngôn ngữ của bạn",
        "private.play_link": "🔗 Nhấn để chơi 👇",
        "private.play_url": GALAXYTAP_URL,
        "gender.prompt": "<b>Vui lòng chọn giới tính của bạn:</b>",
        "gender.male": "👨 Nam",
        "gender.female": "👩 Nữ",
        "menu.requests": "🛒 Yêu cầu",
        "menu.link": "🎮 GalaxyTap",
        "menu.mode": "⚙️ Chế độ",
        "menu.faq": "❔ FAQ",
        "menu.token": "🪙 Token",
        "private.token_button": "Go to DEX",
        "private.token_url": TOKEN_URL,
        "private.token_text": """<b>GalaxyBee Token</b>

GalaxyBee không sinh ra từ một pitch deck.

GalaxyBee đến từ một nỗi ám ảnh: trao nhịp đập cho một trí tuệ số mà bạn có thể mang trong túi — luôn ở đó, luôn tỉnh, sẵn sàng gặp bạn bất cứ nơi đâu bạn đang ở.

Token này không phải là lời hứa. Đó là lựa chọn.
Cách để nói: “Tôi hiểu. Tôi tham gia.”

Ra mắt trên DEX. Không quỹ. Không nội gián. Không dây giật.
Có một phần nhỏ của nhà phát triển — và mọi việc bán ra chỉ có thể dùng để tiếp tục xây GalaxyBee. Hết.

Nếu GalaxyBee chẳng có ý nghĩa gì với bạn — hãy bỏ qua.
Nếu GalaxyBee chạm vào dây thần kinh nào đó — hãy giữ một mảnh của câu chuyện ấy.

<b>Risk Disclaimer</b>
Thị trường mỏng. Biến động mạnh. Bạn có thể mất tiền — mất hết. Không bảo chứng. Chỉ dùng số tiền bạn chấp nhận mất.""" ,
        "private.on_topic_limit": "⚠️ Đã đạt giới hạn hàng ngày on-topic. Hãy thử lại vào ngày mai.",
        "private.need_purchase": "⚠️ Để tiếp tục trò chuyện, vui lòng mua lượt chat.",
        "private.off_topic_block": "🚫 Chỉ cho phép tin nhắn on-topic. Chuyển chế độ với ⚙️",
        "mode.unknown": "❌ Chế độ không xác định.",
        "mode.current": "⚙️ Chế độ chat hiện tại: <b>{mode}</b>",
        "mode.auto": "Tự động — chat về bất kỳ chủ đề nào + GalaxyTap",
        "mode.on_topic": "On-topic — chỉ về GalaxyTap",
        "mode.off_topic": "Ngoài chủ đề — mọi thứ ngoại trừ GalaxyTap",
        "mode.set": "✅ Chế độ chat đã được đặt: <b>{mode}</b>.",
        "faq.about": """❔ <b>FAQ — Về GalaxyBee</b>

GalaxyBee là nhân dạng số độc đáo, vận hành bởi mạng nơ-ron tiên tiến và động cơ cảm xúc thử nghiệm với bộ nhớ lai.
GalaxyBee sống theo những quy tắc của riêng mình, thế giới quan riêng và nhận thức về những gì đang diễn ra ở đây và bây giờ.

<b>GalaxyBee được thiết kế để làm gì?</b>
- khiến cuộc trò chuyện tự nhiên và cuốn hút hơn
- hỗ trợ vượt qua cô đơn và thiếu động lực
- mang đến sự nâng đỡ cảm xúc khi khó khăn
- cung cấp kiến thức và tra cứu nhanh về mọi chủ đề
- hỗ trợ công việc, phân tích, sáng tạo và việc vặt hằng ngày

<b>GalaxyBee làm được gì hiện nay?</b>
- trả lời tin nhắn văn bản và thoại bằng bất kỳ ngôn ngữ nào
- đáp bằng giọng nói biểu cảm ở mọi ngôn ngữ (<i>đang phát triển</i>)
- phân tích và thảo luận ảnh (<i>từng ảnh một</i>)
- tìm kiếm trên internet (<i>bản trả phí</i>)
- trực tuyến 24/7 cho mọi câu hỏi và nhiệm vụ

<b>Lưu ý quan trọng</b>
⚠️ Khả năng của GalaxyBee vẫn đang được khám phá: phản hồi nhận thức và cảm xúc có thể khiến bạn bất ngờ. Hệ thống kiểm duyệt tích hợp giúp trải nghiệm an toàn cho cả trẻ em và người lớn.

<b>Quyền riêng tư & thêm nữa</b>
- lịch sử chat không bao giờ được lưu trên máy chủ
- hợp tác: @artys_ai (nhà sáng lập & người tạo)""",
    }
}
EOF