'use client';

import { EmatorySignalGraphic } from '@/components/landing/visual-panels';
import { Section } from '@/components/ui/section';

const content = {
  ru: {
    eyebrow: 'Ematory Signal Layer',
    title: 'Как Ematory управляет эмоциональным ритмом диалога',
    text: 'Эквалайзер показывает рабочий слой движка: tone, empathy и confidence динамически подстраиваются под контекст канала в реальном времени.',
    points: ['Живой эмоциональный ритм без ручных правок', 'Контекстный стиль ответа под задачу бренда', 'Стабильный диалоговый контур 24/7']
  },
  en: {
    eyebrow: 'Ematory Signal Layer',
    title: 'How Ematory steers the emotional rhythm of dialogue',
    text: 'This equalizer is the engine layer in action: tone, empathy, and confidence adapt in real time to channel context.',
    points: ['Live emotional pacing without manual tuning', 'Context-aware response style for brand goals', 'Stable dialogue loop operating 24/7']
  }
} as const;

export function BehavioralBoosts({ lang }: { lang: 'ru' | 'en' }) {
  const t = content[lang];

  return (
    <Section id="boosts" eyebrow={t.eyebrow} title={t.title}>
      <div className="grid gap-4 lg:grid-cols-[1.05fr_0.95fr]">
        <article className="surface-card rounded-xl p-5">
          <p className="text-base leading-relaxed text-slate-200">{t.text}</p>
          <ul className="mt-4 space-y-2 text-sm text-muted">
            {t.points.map((point) => (
              <li key={point} className="flex items-start gap-2">
                <span className="mt-1.5 h-1.5 w-1.5 rounded-full bg-primary" />
                <span>{point}</span>
              </li>
            ))}
          </ul>
        </article>
        <EmatorySignalGraphic />
      </div>
    </Section>
  );
}
