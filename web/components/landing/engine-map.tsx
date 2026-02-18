'use client';

import { useState } from 'react';
import { Section } from '@/components/ui/section';

const content = {
  ru: {
    eyebrow: 'Ematory Engine',
    title: 'Поведенческая оркестрация: от входящего сигнала до уместного действия',
    label: 'Узел',
    explain: 'Как Ematory собирает контекст',
    nodes: [
      { id: 'tg-api', title: 'Telegram / API ingress', hint: 'Входящие события', copy: 'Входящие события из Telegram и внешних API собираются в единый ingest-контур с идемпотентной обработкой.' },
      { id: 'brain', title: 'Ядро персоны', hint: 'Память + контекст', copy: 'Контекст, память и стиль ответа формируют релевантную реакцию для личных и групповых сценариев.' },
      { id: 'ops', title: 'Операционный слой', hint: 'Модерация + выплаты', copy: 'Модерация, платежи, контент и воркеры очередей стабилизируют продакшн-нагрузку и качество сервиса.' }
    ]
  },
  en: {
    eyebrow: 'Ematory Engine',
    title: 'Behavioral orchestration: from incoming signal to context-aware action',
    label: 'Node',
    explain: 'How Ematory composes context',
    nodes: [
      { id: 'tg-api', title: 'Telegram / API ingress', hint: 'Incoming events', copy: 'Incoming Telegram and external API events are merged into one ingest contour with idempotent processing.' },
      { id: 'brain', title: 'Persona Brain', hint: 'Memory + context', copy: 'Context, memory, and response style rules generate relevant outputs for private and group flows.' },
      { id: 'ops', title: 'Operations Layer', hint: 'Moderation + payments', copy: 'Moderation, payments, content workflows, and queue workers keep production load and quality under control.' }
    ]
  }
} as const;

export function EngineMap({ lang }: { lang: 'ru' | 'en' }) {
  const t = content[lang];
  const [activeId, setActiveId] = useState<string>(t.nodes[0].id);
  const activeNode = t.nodes.find((node) => node.id === activeId) ?? t.nodes[0];

  return (
    <Section eyebrow={t.eyebrow} title={t.title}>
      <div className="grid gap-4 md:grid-cols-3">
        {t.nodes.map((node, index) => (
          <button
            key={node.id}
            type="button"
            onFocus={() => setActiveId(node.id)}
            onMouseEnter={() => setActiveId(node.id)}
            className={`relative flex min-h-[106px] flex-col justify-between overflow-hidden rounded-xl border p-4 text-left transition-colors ${node.id === activeId ? 'border-primary bg-surface/80 shadow-glow' : 'border-slate-700/60 bg-surface/30'}`}
          >
            <div className="pointer-events-none absolute -right-6 -top-6 h-20 w-20 rounded-full bg-primary/10 blur-xl" />
            <p className="text-xs uppercase tracking-[0.16em] text-muted">{t.label} {index + 1}</p>
            <p className="text-xl font-semibold leading-tight">{node.title}</p>
            <p className="mt-1 text-xs text-muted">{node.hint}</p>
          </button>
        ))}
      </div>

      <article className="surface-card mt-6 rounded-xl p-5">
        <p className="text-xs uppercase tracking-[0.16em] text-primary">{t.explain}</p>
        <p className="mt-2 max-w-3xl text-lg leading-relaxed">{activeNode.copy}</p>
      </article>
    </Section>
  );
}
