'use client';

import { Section } from '@/components/ui/section';

type Row = {
  criterion: string;
  synchatica: string;
  classicBot: string;
  cxSuite: string;
};

const content: Record<'ru' | 'en', { eyebrow: string; title: string; subtitle: string; columns: [string, string, string, string]; rows: Row[] }> = {
  ru: {
    eyebrow: 'Сравнение',
    title: 'Почему Synchatica сильнее шаблонных чат-решений',
    subtitle: 'Фокус: p2p-взаимодействие в бренд-каналах, эмоциональная связка и снижение ручного ops 24/7.',
    columns: ['Критерий', 'Synchatica', 'Классические боты', 'CX-suite'],
    rows: [
      { criterion: 'Естественный p2p-диалог в каналах', synchatica: 'Высоко', classicBot: 'Низко', cxSuite: 'Средне' },
      { criterion: 'Контекст + память + тон бренда', synchatica: 'Высоко', classicBot: 'Низко', cxSuite: 'Средне' },
      { criterion: 'Снижение ручной модерации/коммуникаций', synchatica: 'Сильное', classicBot: 'Частично', cxSuite: 'Частично' },
      { criterion: 'Осведомление и вовлечение 24/7', synchatica: 'Системно', classicBot: 'Эпизодически', cxSuite: 'По процессу' }
    ]
  },
  en: {
    eyebrow: 'Comparison',
    title: 'Why Synchatica outperforms template chatbot stacks',
    subtitle: 'Focus: p2p interactions in brand channels, emotional affinity, and lower manual ops 24/7.',
    columns: ['Criterion', 'Synchatica', 'Classic bots', 'CX-suite'],
    rows: [
      { criterion: 'Natural p2p dialogue in channels', synchatica: 'High', classicBot: 'Low', cxSuite: 'Medium' },
      { criterion: 'Context + memory + brand tone', synchatica: 'High', classicBot: 'Low', cxSuite: 'Medium' },
      { criterion: 'Reduced manual moderation/communications', synchatica: 'Strong', classicBot: 'Partial', cxSuite: 'Partial' },
      { criterion: 'Awareness and engagement 24/7', synchatica: 'Systemic', classicBot: 'Episodic', cxSuite: 'Process-led' }
    ]
  }
};

export function ComparisonTable({ lang }: { lang: 'ru' | 'en' }) {
  const t = content[lang];

  return (
    <Section id="comparison" eyebrow={t.eyebrow} title={t.title}>
      <p className="mb-4 max-w-3xl text-sm text-muted md:text-base">{t.subtitle}</p>
      <div className="surface-card overflow-hidden rounded-xl">
        <div className="grid grid-cols-4 border-b border-slate-700/60 bg-slate-950/40 text-xs uppercase tracking-[0.12em] text-muted">
          {t.columns.map((column) => (
            <div key={column} className="px-4 py-3">{column}</div>
          ))}
        </div>
        {t.rows.map((row, idx) => (
          <div key={row.criterion} className={`grid grid-cols-4 text-sm ${idx !== t.rows.length - 1 ? 'border-b border-slate-800/80' : ''}`}>
            <div className="px-4 py-3.5 text-slate-200">{row.criterion}</div>
            <div className="px-4 py-3.5 text-primary">{row.synchatica}</div>
            <div className="px-4 py-3.5 text-muted">{row.classicBot}</div>
            <div className="px-4 py-3.5 text-muted">{row.cxSuite}</div>
          </div>
        ))}
      </div>
    </Section>
  );
}
