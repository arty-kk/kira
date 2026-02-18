'use client';

import { useEffect, useRef, useState } from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'motion/react';
import gsap from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { Section } from '@/components/ui/section';

gsap.registerPlugin(ScrollTrigger);

type ShiftPair = {
  label: string;
  signal: string;
  before: string;
  after: string;
  impact: string;
  beforeNotes: string[];
  afterNotes: string[];
};

const pairs: Record<'ru' | 'en', ShiftPair[]> = {
  ru: [
    {
      label: 'Вовлечение',
      signal: 'Адаптивное приветствие + реактивация + сценарии удержания',
      before: 'Разовые рассылки без контекста',
      after: 'Персональные циклы общения и следующее действие по сигналам поведения',
      impact: 'Ret +19% / WAU depth',
      beforeNotes: ['Нет сегментации по намерению', 'Однотипный контент для всех'],
      afterNotes: ['Сценарий выбирается по сигналам', 'Персональные триггеры возврата']
    },
    {
      label: 'Операции',
      signal: 'Модерация + очереди + безопасный ответ без эскалации',
      before: 'Ручная модерация и реакции по факту',
      after: 'Автоматизированный операционный контур с предсказуемой нагрузкой и стабильным SLA',
      impact: 'Ops load -34%',
      beforeNotes: ['Ручной разбор инцидентов', 'Пики нагрузки без приоритизации'],
      afterNotes: ['Очереди и правила приоритета', 'Контролируемый безопасный ответ']
    },
    {
      label: 'Монетизация',
      signal: 'Монетизация Stars + сценарии офферов + контроль ошибок',
      before: 'Платежные триггеры без сегментации',
      after: 'Динамические офферы по стадии пользователя с управляемой конверсией',
      impact: 'Stars ARPPU +27%',
      beforeNotes: ['Оффер не учитывает стадию', 'Слабая связка с удержанием'],
      afterNotes: ['Оффер привязан к поведенческому этапу', 'Контур конверсии с проверкой ошибок']
    }
  ],
  en: [
    {
      label: 'Engagement',
      signal: 'Adaptive welcome + re-engagement + retention loops',
      before: 'One-off broadcasts without context',
      after: 'Personal communication loops and next-best action by behavior signals',
      impact: 'Ret +19% / WAU depth',
      beforeNotes: ['No intent-based segmentation', 'Same feed for every user'],
      afterNotes: ['Scenario selected by user signals', 'Personal return triggers']
    },
    {
      label: 'Operations',
      signal: 'Moderation + queues + safe response without escalation',
      before: 'Manual moderation and reactive handling',
      after: 'Automated operations contour with predictable load and stable SLA',
      impact: 'Ops load -34%',
      beforeNotes: ['Manual incident triage', 'Load spikes without prioritization'],
      afterNotes: ['Queue routing with priorities', 'Controlled safe-fallback behavior']
    },
    {
      label: 'Monetization',
      signal: 'Stars + offer scenarios + error control',
      before: 'Payment prompts without segmentation',
      after: 'Dynamic offers by lifecycle stage with managed conversion',
      impact: 'Stars ARPPU +27%',
      beforeNotes: ['Offer ignores lifecycle stage', 'Weak retention-to-revenue linkage'],
      afterNotes: ['Offers tied to behavior stage', 'Conversion contour with error control']
    }
  ]
};

export function ProblemShift({ lang }: { lang: 'ru' | 'en' }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useReducedMotion();
  const [active, setActive] = useState(0);
  const data = pairs[lang];

  useEffect(() => {
    if (reducedMotion || !containerRef.current) return;
    const cards = containerRef.current.querySelectorAll('[data-shift-item]');
    gsap.fromTo(
      cards,
      { opacity: 0.6, y: 16 },
      {
        opacity: 1,
        y: 0,
        stagger: 0.1,
        duration: 0.35,
        scrollTrigger: { trigger: containerRef.current, start: 'top 82%' }
      }
    );
  }, [reducedMotion]);

  useEffect(() => {
    if (reducedMotion) return;
    const timer = window.setInterval(() => setActive((prev) => (prev + 1) % data.length), 4000);
    return () => window.clearInterval(timer);
  }, [data.length, reducedMotion]);

  return (
    <Section
      id="shift"
      eyebrow={lang === 'ru' ? 'Проблема → Сдвиг' : 'Problem → Shift'}
      title={lang === 'ru' ? 'От хаотичных ответов к управляемому росту через сценарии и контекст' : 'From chaotic bot replies to managed growth through scenarios and context'}
    >
      <div ref={containerRef} className="grid gap-4 md:grid-cols-3">
        {data.map((pair, idx) => (
          <button
            key={pair.label}
            type="button"
            data-shift-item
            onMouseEnter={() => setActive(idx)}
            onFocus={() => setActive(idx)}
            className={`surface-card group relative flex min-h-[138px] flex-col overflow-hidden rounded-xl p-5 text-left transition-all ${
              active === idx ? 'border-primary shadow-glow' : 'opacity-90 hover:opacity-100'
            }`}
          >
            <div className="pointer-events-none absolute right-0 top-0 h-20 w-20 bg-[radial-gradient(circle,rgba(68,232,197,0.24),transparent_72%)]" />
            <div className="flex items-start justify-between gap-2">
              <p className="text-xs uppercase tracking-[0.16em] text-primary">{pair.label}</p>
              <div className="inline-flex w-fit rounded-md border border-primary/30 bg-slate-950/35 px-2 py-1 text-[10px] uppercase tracking-[0.12em] text-primary/90">
                {pair.impact}
              </div>
            </div>
            <p className="mt-3 text-sm leading-relaxed text-muted">{pair.signal}</p>

            <div className="mt-auto pt-4">
              <div className="h-px bg-gradient-to-r from-transparent via-primary/35 to-transparent" />
              <div className="mt-2 flex items-center gap-1.5">
                {[0, 1, 2, 3, 4].map((dot) => (
                  <motion.span
                    key={dot}
                    className="h-1.5 w-1.5 rounded-full bg-primary/70"
                    animate={
                      reducedMotion
                        ? false
                        : {
                            opacity: active === idx ? [0.35, 1, 0.35] : 0.3,
                            scale: active === idx ? [0.9, 1.2, 0.9] : 0.9
                          }
                    }
                    transition={{ duration: 1.2, repeat: Number.POSITIVE_INFINITY, delay: dot * 0.1 }}
                  />
                ))}
              </div>
            </div>

            {active === idx ? (
              <motion.div
                layoutId="shift-active-underline"
                className="absolute inset-x-4 bottom-0 h-[2px] rounded-full bg-primary"
                transition={{ type: 'spring', stiffness: 320, damping: 28 }}
              />
            ) : null}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        <motion.div
          key={active}
          initial={reducedMotion ? false : { opacity: 0, y: 10 }}
          animate={{ opacity: 1, y: 0 }}
          exit={reducedMotion ? {} : { opacity: 0, y: -6 }}
          transition={{ duration: 0.22 }}
          className="mt-5 grid gap-4 md:grid-cols-2"
        >
          <article className="surface-card rounded-xl border-slate-600/40 p-5">
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs uppercase tracking-[0.16em] text-muted">{lang === 'ru' ? 'Было' : 'Before'}</p>
              <span className="rounded-md border border-slate-600/60 bg-slate-900/45 px-2 py-1 text-[10px] uppercase tracking-[0.12em] text-slate-300">
                {lang === 'ru' ? 'Реактивно' : 'Reactive'}
              </span>
            </div>
            <p className="mt-2 text-xl font-medium leading-snug">{data[active].before}</p>
            <ul className="mt-3 space-y-2">
              {data[active].beforeNotes.map((note) => (
                <li key={note} className="flex items-start gap-2 text-sm text-muted">
                  <span className="mt-1 h-1.5 w-1.5 rounded-full bg-slate-500" />
                  <span>{note}</span>
                </li>
              ))}
            </ul>
          </article>
          <article className="surface-card rounded-xl border-primary/55 p-5">
            <div className="flex items-center justify-between gap-3">
              <p className="text-xs uppercase tracking-[0.16em] text-primary">{lang === 'ru' ? 'Стало' : 'After'}</p>
              <span className="rounded-md border border-primary/45 bg-primary/10 px-2 py-1 text-[10px] uppercase tracking-[0.12em] text-primary">
                {lang === 'ru' ? 'Управляемо' : 'Managed'}
              </span>
            </div>
            <p className="mt-2 text-xl font-medium leading-snug">{data[active].after}</p>
            <ul className="mt-3 space-y-2">
              {data[active].afterNotes.map((note) => (
                <li key={note} className="flex items-start gap-2 text-sm text-primary/90">
                  <span className="mt-1 h-1.5 w-1.5 rounded-full bg-primary" />
                  <span>{note}</span>
                </li>
              ))}
            </ul>
          </article>
        </motion.div>
      </AnimatePresence>
    </Section>
  );
}
