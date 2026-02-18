'use client';

import dynamic from 'next/dynamic';
import { motion, useReducedMotion } from 'motion/react';
import { Button } from '@/components/ui/button';
import { trackEvent } from '@/lib/analytics';

const DynamicHeroScene = dynamic(
  () => import('@/components/landing/hero-scene').then((mod) => mod.HeroScene),
  {
    ssr: false,
    loading: () => (
      <div className="relative h-full w-full overflow-hidden rounded-[1.5rem]">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_50%_35%,_#44E8C53B,_transparent_58%)]" />
        <div className="absolute inset-0 bg-[linear-gradient(160deg,rgba(68,232,197,0.12),rgba(7,12,24,0.8)_58%)]" />
      </div>
    )
  }
);

const copy = {
  ru: {
    eyebrow: 'Платформа ИИ-персоны для сообществ и digital-продуктов',
    title: 'Synchatica превращает Telegram и API-каналы в управляемую AI-выручку',
    text: 'Welcome, re-engagement, battle-механики, монетизация через Stars, модерация и очереди — в одном продакшн-контуре без ручной перегрузки команды.',
    ctaMain: 'Запросить демо',
    ctaAlt: 'Как это работает',
    m1: 'Монетизация',
    m2: 'Удержание',
    m3: 'Ops-нагрузка',
    pillars: ['Adaptive Emotionality', 'AI Reasoning', 'Multilevel Memory', 'Hybrid RAG']
  },
  en: {
    eyebrow: 'AI persona platform for communities and digital products',
    title: 'Synchatica turns Telegram and API channels into managed AI revenue',
    text: 'Welcome, re-engagement, battle mechanics, Telegram Stars monetization, moderation, and queue operations — all in one production-ready control layer.',
    ctaMain: 'Request demo',
    ctaAlt: 'See how it works',
    m1: 'Monetization',
    m2: 'Retention',
    m3: 'Ops load',
    pillars: ['Adaptive Emotionality', 'AI Reasoning', 'Multilevel Memory', 'Hybrid RAG']
  }
} as const;

export function Hero({ lang }: { lang: 'ru' | 'en' }) {
  const reducedMotion = useReducedMotion();
  const t = copy[lang];

  const handleSeeHow = () => {
    trackEvent('cta_see_how');
    document.getElementById('shift')?.scrollIntoView({ behavior: reducedMotion ? 'auto' : 'smooth' });
  };

  return (
    <section className="relative overflow-hidden section-divider">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_82%_16%,_rgba(68,232,197,0.24),_transparent_46%)]" />

      <div className="mx-auto grid min-h-[52vh] w-full max-w-6xl gap-8 px-6 pb-6 pt-4 md:grid-cols-2 md:items-center md:pb-10 md:pt-6">
        <motion.div
          initial={reducedMotion ? false : { opacity: 0, y: 14 }}
          animate={reducedMotion ? {} : { opacity: 1, y: 0 }}
          transition={{ duration: 0.38 }}
          className="relative z-10 space-y-5"
        >
          <p className="text-xs uppercase tracking-[0.2em] text-primary">{t.eyebrow}</p>
          <h1 className="max-w-[13ch] text-4xl font-semibold leading-[1.03] tracking-tight md:text-[3.35rem]">{t.title}</h1>
          <p className="max-w-xl text-base leading-relaxed text-muted md:text-lg">{t.text}</p>
          <div className="flex flex-wrap gap-3">
            <Button onClick={() => trackEvent('cta_request_demo')}>{t.ctaMain}</Button>
            <Button variant="ghost" onClick={handleSeeHow}>{t.ctaAlt}</Button>
          </div>
          <div className="flex max-w-xl flex-wrap gap-2 text-[11px] uppercase tracking-[0.12em] text-slate-300">
            {t.pillars.map((pillar) => (
              <span key={pillar} className="rounded-full border border-primary/30 bg-primary/10 px-3 py-1">{pillar}</span>
            ))}
          </div>
          <div className="grid max-w-lg grid-cols-3 gap-3 text-xs text-muted">
            <div className="surface-card rounded-lg p-3"><p className="text-primary">{t.m1}</p><p className="mt-1">+27%</p></div>
            <div className="surface-card rounded-lg p-3"><p className="text-primary">{t.m2}</p><p className="mt-1">+19%</p></div>
            <div className="surface-card rounded-lg p-3"><p className="text-primary">{t.m3}</p><p className="mt-1">-34%</p></div>
          </div>
        </motion.div>

        <div className="relative z-10 h-72 overflow-hidden rounded-[1.5rem] bg-[linear-gradient(170deg,rgba(18,31,58,0.74),rgba(7,11,22,0.95))] shadow-[0_24px_96px_rgba(0,0,0,0.45)] ring-1 ring-primary/10 md:h-[430px]">
          <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_60%_22%,rgba(68,232,197,0.18),transparent_48%)]" />
          <div className="pointer-events-none absolute inset-x-10 top-4 h-px bg-gradient-to-r from-transparent via-primary/50 to-transparent" />
          {reducedMotion ? <div className="h-full w-full bg-[radial-gradient(circle_at_52%_30%,_#44E8C544,_#0A0D14)]" /> : <DynamicHeroScene />}
        </div>
      </div>
    </section>
  );
}
