'use client';

import { motion, useReducedMotion } from 'motion/react';
import { Section } from '@/components/ui/section';

type Metric = {
  name: string;
  value: string;
  progress: number;
  note: string;
  trend: 'up' | 'down' | 'stable';
};

const content: Record<'ru' | 'en', { eyebrow: string; title: string; metrics: Metric[] }> = {
  ru: {
    eyebrow: 'KPI влияние',
    title: 'Метрики эффекта: выручка, удержание, операционная эффективность и надежность',
    metrics: [
      { name: 'Монетизация', value: '+27%', progress: 82, note: 'Рост платящих сегментов и ARPPU', trend: 'up' },
      { name: 'Удержание', value: '+19%', progress: 74, note: 'Возврат и глубина диалога', trend: 'up' },
      { name: 'Ops-нагрузка', value: '-34%', progress: 66, note: 'Снижение ручных операций', trend: 'down' },
      { name: 'Надежность', value: '99.95%', progress: 96, note: 'Стабильность API и очередей', trend: 'stable' }
    ]
  },
  en: {
    eyebrow: 'KPI Impact',
    title: 'Impact metrics: revenue, retention, ops efficiency, and reliability',
    metrics: [
      { name: 'Monetization', value: '+27%', progress: 82, note: 'Paid segment and ARPPU growth', trend: 'up' },
      { name: 'Retention', value: '+19%', progress: 74, note: 'Return rate and dialogue depth', trend: 'up' },
      { name: 'Ops load', value: '-34%', progress: 66, note: 'Reduction of manual operations', trend: 'down' },
      { name: 'Reliability', value: '99.95%', progress: 96, note: 'API and queue stability', trend: 'stable' }
    ]
  }
};

function trendLabel(lang: 'ru' | 'en', trend: Metric['trend']) {
  if (lang === 'ru') return trend === 'up' ? 'Рост' : trend === 'down' ? 'Снижение' : 'Стабильно';
  return trend === 'up' ? 'Growth' : trend === 'down' ? 'Reduction' : 'Stable';
}

export function KpiImpact({ lang }: { lang: 'ru' | 'en' }) {
  const reducedMotion = useReducedMotion();
  const t = content[lang];

  return (
    <Section id="kpi" eyebrow={t.eyebrow} title={t.title}>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {t.metrics.map((metric, idx) => (
          <motion.article
            key={metric.name}
            initial={false}
            whileInView={reducedMotion ? {} : { y: [10, 0], opacity: [0.82, 1] }}
            transition={{ duration: 0.35, delay: idx * 0.06 }}
            viewport={{ once: true, amount: 0.2 }}
            className="surface-card relative flex min-h-[190px] flex-col overflow-hidden rounded-xl p-5"
          >
            <div className="pointer-events-none absolute inset-x-0 top-0 h-[2px] bg-gradient-to-r from-transparent via-primary/80 to-transparent" />

            <div className="relative z-10 flex items-center justify-between gap-2">
              <p className="text-xs uppercase tracking-[0.15em] text-muted">{metric.name}</p>
              <span className="rounded-full border border-primary/30 bg-primary/10 px-2 py-0.5 text-[10px] uppercase tracking-[0.14em] text-primary">
                {trendLabel(lang, metric.trend)}
              </span>
            </div>

            <p className="relative z-10 mt-4 text-4xl font-semibold leading-none text-primary">{metric.value}</p>
            <p className="relative z-10 mt-3 min-h-[44px] text-sm leading-relaxed text-muted">{metric.note}</p>

            <div className="relative z-10 mt-4">
              <div className="mb-2 flex gap-1.5">
                <span className="h-1.5 w-1.5 rounded-full bg-primary/80" />
                <span className="h-1.5 w-1.5 rounded-full bg-primary/60" />
                <span className="h-1.5 w-1.5 rounded-full bg-primary/40" />
              </div>
              <div className="h-2 rounded-full bg-slate-800/90">
                <motion.div
                  initial={reducedMotion ? false : { width: 0 }}
                  whileInView={reducedMotion ? {} : { width: `${metric.progress}%` }}
                  viewport={{ once: true }}
                  transition={{ duration: 0.55, delay: idx * 0.08 }}
                  className="h-2 rounded-full bg-gradient-to-r from-primary to-accent"
                />
              </div>
            </div>
          </motion.article>
        ))}
      </div>
    </Section>
  );
}
