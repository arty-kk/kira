'use client';

import { useState } from 'react';
import Link from 'next/link';
import { AmbientBackground } from '@/components/landing/ambient-background';
import { CapabilityProof } from '@/components/landing/capability-proof';
import { EngineMap } from '@/components/landing/engine-map';
import { FinalCta } from '@/components/landing/final-cta';
import { Hero } from '@/components/landing/hero';
import { KpiImpact } from '@/components/landing/kpi-impact';
import { ProblemShift } from '@/components/landing/problem-shift';
import { ScrollAnalytics } from '@/components/landing/scroll-analytics';

type Lang = 'ru' | 'en';

const copy = {
  ru: {
    product: 'Продукт',
    scenarios: 'Сценарии',
    metrics: 'Метрики',
    contact: 'Контакты',
    requestDemo: 'Запросить демо',
    privacy: 'Privacy',
    terms: 'Terms',
    footerLead: 'Synchatica — чат-персоны с AI и Ematory для диалогов, вовлечения и модерации 24/7.',
    capabilityLine: 'Adaptive Emotionality · Artificial Intelligence · Multilevel Memory · Hybrid RAG',
    contacts: 'support@synchatica.com · @synchatica',
    copyright: '© 2025 Synchatica. All rights reserved.'
  },
  en: {
    product: 'Product',
    scenarios: 'Scenarios',
    metrics: 'Metrics',
    contact: 'Contact',
    requestDemo: 'Request demo',
    privacy: 'Privacy',
    terms: 'Terms',
    footerLead: 'Synchatica builds AI + Ematory chat personas for dialogue, engagement, and moderation 24/7.',
    capabilityLine: 'Adaptive Emotionality · Artificial Intelligence · Multilevel Memory · Hybrid RAG',
    contacts: 'support@synchatica.com · @synchatica',
    copyright: '© 2025 Synchatica. All rights reserved.'
  }
} as const;

export default function HomePage() {
  const [lang, setLang] = useState<Lang>('en');
  const t = copy[lang];

  return (
    <main>
      <AmbientBackground />
      <ScrollAnalytics />

      <header className="mx-auto mt-3 flex w-full max-w-6xl items-center justify-between gap-3 rounded-2xl border border-slate-700/60 bg-slate-950/55 px-4 py-3 backdrop-blur md:px-6">
        <Link href="/" className="text-sm font-semibold tracking-[0.16em] text-primary">SYNCHATICA</Link>

        <nav className="hidden items-center gap-1 rounded-xl border border-slate-700/50 bg-slate-900/45 p-1 text-sm text-muted md:flex">
          <a className="rounded-lg px-3 py-1.5 hover:bg-slate-800/80 hover:text-slate-100" href="#shift">{t.product}</a>
          <a className="rounded-lg px-3 py-1.5 hover:bg-slate-800/80 hover:text-slate-100" href="#capability">{t.scenarios}</a>
          <a className="rounded-lg px-3 py-1.5 hover:bg-slate-800/80 hover:text-slate-100" href="#kpi">{t.metrics}</a>
          <a className="rounded-lg px-3 py-1.5 hover:bg-slate-800/80 hover:text-slate-100" href="#final-cta">{t.contact}</a>
        </nav>

        <div className="flex items-center gap-2">
          <a href="#final-cta" className="hidden rounded-lg border border-primary/35 bg-primary/10 px-3 py-1.5 text-xs font-medium text-primary md:inline-flex">{t.requestDemo}</a>
          <div className="flex items-center gap-2 rounded-lg border border-slate-700/70 bg-slate-950/40 p-1 text-xs">
            <button
              type="button"
              onClick={() => setLang('ru')}
              className={`rounded px-2 py-1 ${lang === 'ru' ? 'bg-primary text-slate-950' : 'text-muted'}`}
            >
              RU
            </button>
            <button
              type="button"
              onClick={() => setLang('en')}
              className={`rounded px-2 py-1 ${lang === 'en' ? 'bg-primary text-slate-950' : 'text-muted'}`}
            >
              EN
            </button>
          </div>
        </div>
      </header>

      <Hero lang={lang} />
      <ProblemShift lang={lang} />
      <EngineMap lang={lang} />
      <CapabilityProof lang={lang} />
      <KpiImpact lang={lang} />
      <FinalCta lang={lang} />

      <footer className="mx-auto mt-8 w-full max-w-6xl section-divider px-6 py-8 text-sm text-muted">
        <div className="grid gap-6 md:grid-cols-[1fr_auto] md:items-end">
          <div>
            <p className="text-sm text-slate-200">{t.footerLead}</p>
            <p className="mt-2 text-[11px] uppercase tracking-[0.16em] text-primary/85">{t.capabilityLine}</p>
            <p className="mt-3 text-xs text-slate-300/90">{t.contacts}</p>
            <p className="mt-3 text-xs">{t.copyright}</p>
          </div>
          <nav className="flex items-center gap-4 text-xs uppercase tracking-[0.12em]">
            <Link href="/privacy">{t.privacy}</Link>
            <Link href="/terms">{t.terms}</Link>
          </nav>
        </div>
      </footer>
    </main>
  );
}
