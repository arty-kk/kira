# Synchatica Landing RnD Blueprint (Codex-ready)

> Этот документ — **исполняемое ТЗ для Codex**.
> Когда захочешь реализацию, в промпте можно просто написать:
> **«Реализуй лендинг по `docs/landing_rnd_strategy_ru.md` без отклонений»**.

---

## 0) Цель документа

Сделать план не «про идеи», а про **точную реализацию**:
- что именно создавать;
- в каком порядке;
- какие файлы и компоненты;
- какие проверки и критерии готовности;
- как контролировать, чтобы лендинг был динамичным, ярким и без инфо-перегруза.

---

## 1) Контекст проекта (аудит, кратко)

- Текущий репозиторий — backend-centric платформа (бот/API/воркеры/очереди).
- Выделенного production-ready frontend-лендинга нет.
- Значит, лендинг делаем **отдельным модулем `web/`**, без вмешательства в текущие backend-контракты.

---

## 2) Стек для реализации (зафиксировать без самодеятельности)

## 2.1 Обязательный стек
- **Next.js 14 + TypeScript + App Router**.
- **Tailwind CSS** + дизайн-токены через CSS variables.
- **Motion (Framer Motion API)** для анимаций компонентов.
- **GSAP + ScrollTrigger** для скролл-сцен.
- **React Three Fiber + drei** только для Hero-сцены.

## 2.2 Почему именно так
- Дает премиальный visual/motion-уровень для AI RnD-позиционирования.
- Позволяет быстро делать A/B варианты без ломки архитектуры.
- Удерживает хороший SEO/Performance (SSR + контроль ассетов).

## 2.3 Официальные источники для Codex (использовать при реализации)
- Next.js docs: https://nextjs.org/docs
- Motion React docs: https://motion.dev/docs/react
- GSAP ScrollTrigger docs: https://gsap.com/docs/v3/Plugins/ScrollTrigger/
- React Three Fiber docs: https://r3f.docs.pmnd.rs/getting-started/introduction
- Tailwind docs: https://tailwindcss.com/docs

---

## 3) Продуктовый принцип лендинга

Формула: **wow-эмоция + ясность + короткий путь к действию**.

- 1 экран = 1 мысль.
- Минимум текста, максимум визуальных доказательств.
- Главная цель: `Request demo`.
- Вторичная цель: `See how it works`.

---

## 4) Информационная архитектура (строго)

## Экран 1 — Hero
- H1: Synchatica как AI Persona Engine.
- Подзаголовок: 1 короткое value statement.
- Интерактивный визуал (R3F).
- CTA: `Request demo`, `See how it works`.

## Экран 2 — Problem → Shift
- 3 пары «было/стало».
- Анимированный переключатель/transition карточек.

## Экран 3 — Product Engine
- Интерактивная схема узлов:
  - Telegram/API
  - Persona Brain
  - Ops Layer (moderation/payments/content)
- Hover/focus показывает micro-copy (1–2 строки).

## Экран 4 — Capability Proof
- 3–4 мини-сценария (чат-симуляция/таймлайн):
  - welcome;
  - re-engagement;
  - group battle;
  - safe fallback.

## Экран 5 — KPI Impact
- 4 блока: Monetization / Retention / Ops / Reliability.
- Анимированные метрики и мини-графика.

## Экран 6 — Final CTA
- Короткий оффер + форма/кнопка запроса демо.

---

## 5) Техническая декомпозиция для Codex

## 5.1 Структура файлов (MVP)

```text
web/
  app/
    layout.tsx
    page.tsx
    privacy/page.tsx
    terms/page.tsx
    globals.css
  components/
    landing/
      hero.tsx
      problem-shift.tsx
      engine-map.tsx
      capability-proof.tsx
      kpi-impact.tsx
      final-cta.tsx
    ui/
      button.tsx
      section.tsx
      badge.tsx
  lib/
    analytics.ts
    motion.ts
    seo.ts
  public/
    images/
    icons/
  tests/
    smoke.spec.ts
  README.md
```

## 5.2 Правила реализации
- Тяжелые визуалы грузить через dynamic import.
- Hero 3D должен иметь fallback (статичный gradient/noise фон).
- Вся анимация обязана учитывать `prefers-reduced-motion`.
- Никаких блокирующих клиентских скриптов в critical path.

---

## 6) План работ для Codex (по шагам, без двусмысленности)

## STEP 1 — Bootstrap web-модуля
**Сделать**
- Создать `web/` проект на Next.js + TS + Tailwind.
- Прописать базовые npm-скрипты (`dev`, `build`, `start`, `lint`, `test:e2e`).

**DoD**
- `web` собирается локально без ошибок.
- Главная страница открывается.

## STEP 2 — Design tokens и UI-база
**Сделать**
- Завести переменные цветов/типографики/тени/радиусов.
- Добавить базовые компоненты (`Button`, `Section`, `Badge`).

**DoD**
- Единая визуальная система подключена на всех секциях.

## STEP 3 — Контентные секции лендинга
**Сделать**
- Реализовать 6 секций из раздела 4.
- Сделать адаптив desktop/tablet/mobile.

**DoD**
- Весь маршрут `/` собран из переиспользуемых компонентов.

## STEP 4 — Motion и скролл-сцены
**Сделать**
- Подключить Motion для reveal/hover/CTA transitions.
- Подключить GSAP ScrollTrigger для 1–2 ключевых storytelling-сцен.

**DoD**
- Анимации плавные, без дерганий, без визуального шума.

## STEP 5 — Hero R3F
**Сделать**
- Добавить легкую R3F-сцену в hero.
- Реализовать fallback для слабых устройств и reduced-motion.

**DoD**
- Hero выглядит премиально, но не роняет перфоманс.

## STEP 6 — SEO/Analytics/Legal
**Сделать**
- Подключить metadata + OpenGraph + базовый schema.org.
- Добавить analytics hooks для CTA/scroll depth.
- Реализовать `/privacy` и `/terms` страницы.

**DoD**
- Страницы индексируемы, CTA события отправляются.

## STEP 7 — QA и release readiness
**Сделать**
- Smoke e2e (главная загрузка, скролл, CTA).
- Перфоманс-проход и оптимизация ассетов.

**DoD**
- Проект готов к выкатке как MVP-лендинг.

---

## 7) Нефункциональные критерии качества

- Lighthouse Performance (mobile): целевой ориентир **>85**.
- CLS: минимальный (без layout shift в hero/CTA).
- A11y: контраст и навигация с клавиатуры для интерактивных блоков.
- Motion budget:
  - активные «wow»-сцены только в hero + 1 секции;
  - остальные анимации — короткие и функциональные.

---

## 8) Что Codex должен запустить в проверках

Из корня `web/`:
- `npm run lint`
- `npm run build`
- `npm run test:e2e` (или smoke эквивалент)

Если в репозитории добавляется CI для `web/`, зафиксировать job:
- lint → typecheck → build → (опционально) lighthouse-ci.

---

## 9) Риски и защита от них

- Риск «слишком много эффектов» → жесткий motion budget.
- Риск падения FPS → упрощение R3F-сцены + lazy loading.
- Риск инфо-перегруза → лимит текста: 2–3 строки на блок.
- Риск расползания сроков → сначала MVP без сложных экспериментальных шейдеров.

---

## 10) Готовый промпт для следующего запуска Codex

Скопируй и используй дословно:

```text
Реализуй MVP лендинг Synchatica строго по документу docs/landing_rnd_strategy_ru.md.
Ограничения:
1) Не менять backend-контракты и существующие сервисы.
2) Создать отдельный модуль web/ на Next.js + TypeScript + Tailwind.
3) Реализовать секции и шаги из документа без пропусков.
4) Добавить проверку lint/build/e2e smoke.
5) В финале дать отчет: что сделано, какие команды запущены, что не успел.
```

