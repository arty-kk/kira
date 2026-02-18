'use client';

import { useEffect, useState } from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'motion/react';
import { Section } from '@/components/ui/section';

type Scenario = {
  id: string;
  title: string;
  summary: string;
  timeline: Array<{ label: string; text: string }>;
};

const content: Record<
  'ru' | 'en',
  { eyebrow: string; title: string; mode: string; scenarios: Scenario[] }
> = {
  ru: {
    eyebrow: 'Доказательство возможностей',
    title: 'Сценарии продукта: как Synchatica ведёт путь от первого касания к оплате и возврату',
    mode: 'режим исполнения',
    scenarios: [
      {
        id: 'welcome',
        title: 'Приветствие',
        summary:
          'Новый пользователь получает адаптивный вход в диалог, сегментацию и быстрое первое полезное действие.',
        timeline: [
          { label: 'Шаг 1', text: 'Вход в чат и определение контекста' },
          { label: 'Шаг 2', text: 'Персональное приветствие под сегмент' },
          { label: 'Шаг 3', text: 'Первое ценностное действие без трения' },
          { label: 'Шаг 4', text: 'Мягкий переход к оплате/подписке' }
        ]
      },
      {
        id: 're-engagement',
        title: 'Возврат',
        summary:
          'Падение активности трактуется как сигнал: система выбирает триггер и возвращает пользователя в активный цикл.',
        timeline: [
          { label: 'Шаг 1', text: 'Фиксация падения активности' },
          { label: 'Шаг 2', text: 'Определение причины/сегмента' },
          { label: 'Шаг 3', text: 'Персональный триггер возврата' },
          { label: 'Шаг 4', text: 'Возврат в регулярное взаимодействие' }
        ]
      },
      {
        id: 'group-battle',
        title: 'Игровой групповой сценарий',
        summary:
          'Групповой сценарий превращает разовый интерес в повторяемую социальную механику и удержание.',
        timeline: [
          { label: 'Шаг 1', text: 'Запуск события в группе' },
          { label: 'Шаг 2', text: 'Раздача ролей и правил' },
          { label: 'Шаг 3', text: 'Соревновательный раунд' },
          { label: 'Шаг 4', text: 'Повторный заход и ретеншн-луп' }
        ]
      },

      {
        id: 'direct-dialog',
        title: 'Личка: вовлечение',
        summary:
          'В личном диалоге персона развивает интерес через серию контекстных касаний и мягко ведёт к целевому действию.',
        timeline: [
          { label: 'Шаг 1', text: 'Проверка контекста и тона общения' },
          { label: 'Шаг 2', text: 'Персональный вопрос и уточнение интереса' },
          { label: 'Шаг 3', text: 'Полезный ответ с микро-оффером' },
          { label: 'Шаг 4', text: 'Возврат в диалог без давления' }
        ]
      },
      {
        id: 'safe-fallback',
        title: 'Модерация без потери диалога',
        summary:
          'Рискованные сигналы обрабатываются безопасно: диалог сохраняется, а нагрузка не уходит в ручную эскалацию.',
        timeline: [
          { label: 'Шаг 1', text: 'Выявление риск-сигнала' },
          { label: 'Шаг 2', text: 'Проверка правилами безопасности' },
          { label: 'Шаг 3', text: 'Контролируемый fallback-ответ' },
          { label: 'Шаг 4', text: 'Сохранение диалога в целевом контуре' }
        ]
      }
    ]
  },
  en: {
    eyebrow: 'Capability Proof',
    title: 'Scenario proof: how Synchatica moves users from first touch to conversion and return',
    mode: 'execution mode',
    scenarios: [
      {
        id: 'welcome',
        title: 'Welcome',
        summary: 'A new user receives adaptive onboarding, segmentation, and a fast first valuable action.',
        timeline: [
          { label: 'Step 1', text: 'Chat entry and context detection' },
          { label: 'Step 2', text: 'Personal greeting by segment' },
          { label: 'Step 3', text: 'First low-friction value action' },
          { label: 'Step 4', text: 'Soft move to payment/subscription' }
        ]
      },
      {
        id: 're-engagement',
        title: 'Re-engagement',
        summary: 'Activity drop is treated as a signal: the system selects a trigger and returns the user to the active loop.',
        timeline: [
          { label: 'Step 1', text: 'Detect activity decline' },
          { label: 'Step 2', text: 'Identify cause and segment' },
          { label: 'Step 3', text: 'Personal return trigger' },
          { label: 'Step 4', text: 'Return to regular interaction' }
        ]
      },
      {
        id: 'group-battle',
        title: 'Group engagement mode',
        summary: 'Group mode turns one-time interest into a repeatable social dynamic with high engagement.',
        timeline: [
          { label: 'Step 1', text: 'Launch group event' },
          { label: 'Step 2', text: 'Assign roles and rules' },
          { label: 'Step 3', text: 'Run competitive round' },
          { label: 'Step 4', text: 'Repeat entry and retention loop' }
        ]
      },

      {
        id: 'direct-dialog',
        title: 'Direct chat engagement',
        summary: 'In private chat, the persona builds interest through contextual touches and gently guides users to target action.',
        timeline: [
          { label: 'Step 1', text: 'Check context and tone' },
          { label: 'Step 2', text: 'Ask a personal clarification question' },
          { label: 'Step 3', text: 'Deliver value with a micro-offer' },
          { label: 'Step 4', text: 'Return to dialog without pressure' }
        ]
      },
      {
        id: 'safe-fallback',
        title: 'Moderation-safe continuation',
        summary: 'Risky signals are handled safely: the dialog is preserved while avoiding manual escalation overload.',
        timeline: [
          { label: 'Step 1', text: 'Detect risk signal' },
          { label: 'Step 2', text: 'Apply safety policy checks' },
          { label: 'Step 3', text: 'Generate controlled fallback' },
          { label: 'Step 4', text: 'Keep dialog in target flow' }
        ]
      }
    ]
  }
};

export function CapabilityProof({ lang }: { lang: 'ru' | 'en' }) {
  const reducedMotion = useReducedMotion();
  const [active, setActive] = useState(0);
  const t = content[lang];

  useEffect(() => {
    if (reducedMotion) return;
    const timer = window.setInterval(() => setActive((prev) => (prev + 1) % t.scenarios.length), 3800);
    return () => window.clearInterval(timer);
  }, [reducedMotion, t.scenarios.length]);

  return (
    <Section id="capability" eyebrow={t.eyebrow} title={t.title}>
      <div className="grid gap-4 lg:grid-cols-[86px_1fr]">
        <div className="surface-card flex h-fit flex-col gap-2 rounded-xl p-2">
          {t.scenarios.map((scenario, idx) => (
            <button
              key={scenario.id}
              type="button"
              onMouseEnter={() => setActive(idx)}
              onFocus={() => setActive(idx)}
              aria-label={scenario.title}
              className={`flex h-10 w-full items-center justify-center rounded-lg border text-sm font-semibold tracking-[0.08em] transition-all ${
                idx === active
                  ? 'border-primary bg-primary/20 text-primary shadow-glow'
                  : 'border-slate-700/60 bg-surface/30 text-slate-300 hover:border-primary/40 hover:text-slate-100'
              }`}
            >
              {`S${idx + 1}`}
            </button>
          ))}
        </div>

        <div className="surface-card relative flex h-full flex-col overflow-hidden rounded-xl p-5">
          <div className="pointer-events-none absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-transparent via-primary/70 to-transparent" />
          {!reducedMotion ? (
            <motion.div
              key={active}
              initial={{ scaleX: 0 }}
              animate={{ scaleX: 1 }}
              transition={{ duration: 3.6, ease: 'linear' }}
              className="absolute inset-x-0 top-0 h-1 origin-left bg-primary/45"
            />
          ) : null}

          <AnimatePresence mode="wait">
            <motion.div
              key={t.scenarios[active].id}
              initial={reducedMotion ? false : { opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={reducedMotion ? {} : { opacity: 0, y: -8 }}
              transition={{ duration: 0.25 }}
              className="flex h-full flex-col"
            >
              <div className="flex flex-wrap items-center gap-2.5">
                <span className="text-xs uppercase tracking-[0.15em] text-primary/90">{lang === 'ru' ? 'Детали сценария' : 'Scenario details'}</span>
                <span className="text-xs uppercase tracking-[0.15em] text-muted">{t.mode}</span>
                <span className="rounded-md border border-primary/35 bg-primary/10 px-2 py-1 text-[11px] font-medium uppercase tracking-[0.08em] text-primary/90">
                  {`S${active + 1}`}
                </span>
              </div>

              <h3 className="mt-2 text-xl font-semibold leading-snug text-slate-100">{t.scenarios[active].title}</h3>

              <p className="mt-2.5 max-w-2xl text-base leading-relaxed text-slate-200/90">{t.scenarios[active].summary}</p>

              <div className="mt-4 grid gap-2.5 sm:grid-cols-2">
                {t.scenarios[active].timeline.map((step, idx) => (
                  <motion.div
                    key={step.label}
                    initial={reducedMotion ? false : { opacity: 0, x: -8 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ duration: 0.22, delay: idx * 0.06 }}
                    className="flex min-h-[84px] flex-col justify-center rounded-lg border border-slate-700/70 bg-slate-950/35 px-4 py-3"
                  >
                    <p className="text-xs uppercase tracking-[0.14em] text-primary">{step.label}</p>
                    <p className="mt-1 text-base leading-snug">{step.text}</p>
                  </motion.div>
                ))}
              </div>

              <div className="mt-4 pt-1">
                <div className="h-px bg-gradient-to-r from-transparent via-primary/45 to-transparent" />
                <div className="mt-3 flex flex-wrap gap-2 text-[10px] uppercase tracking-[0.13em] text-muted">
                  <span className="rounded border border-primary/30 bg-slate-950/40 px-2 py-1">{lang === 'ru' ? 'Вовлечение' : 'Engagement'}</span>
                  <span className="rounded border border-primary/30 bg-slate-950/40 px-2 py-1">{lang === 'ru' ? 'Контекст' : 'Context'}</span>
                  <span className="rounded border border-primary/30 bg-slate-950/40 px-2 py-1">{lang === 'ru' ? 'Конверсия' : 'Conversion'}</span>
                </div>
              </div>
            </motion.div>
          </AnimatePresence>
        </div>
      </div>
    </Section>
  );
}
