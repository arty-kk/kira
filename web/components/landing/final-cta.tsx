'use client';

import { Button } from '@/components/ui/button';
import { Section } from '@/components/ui/section';
import { trackEvent } from '@/lib/analytics';

const content = {
  ru: {
    eyebrow: 'Финальный шаг',
    title: 'Готовы посмотреть Synchatica на вашем кейсе?',
    text: 'Оставьте рабочий email — покажем, как Synchatica встраивает AI-персону в ваш Telegram/API-контур: память, контекстный тон и управляемая модерация 24/7.',
    placeholder: 'Рабочий email',
    cta: 'Запросить демо'
  },
  en: {
    eyebrow: 'Final CTA',
    title: 'Ready to see Synchatica on your use case?',
    text: 'Leave your work email and we will show how Synchatica plugs an AI persona into your Telegram/API flow with memory, context-aware tone, and managed moderation 24/7.',
    placeholder: 'Work email',
    cta: 'Request demo'
  }
} as const;

export function FinalCta({ lang }: { lang: 'ru' | 'en' }) {
  const t = content[lang];

  return (
    <Section id="final-cta" title={t.title} eyebrow={t.eyebrow}>
      <div className="surface-card rounded-xl border-primary/40 p-6 md:flex md:items-start md:justify-between md:gap-6">
        <div className="max-w-xl">
          <p className="text-base leading-relaxed text-muted">{t.text}</p>
        </div>
        <form className="mt-4 flex w-full max-w-md gap-3 md:mt-0" onSubmit={(event) => event.preventDefault()}>
          <input type="email" placeholder={t.placeholder} aria-label={t.placeholder} className="w-full rounded-lg border border-slate-700/70 bg-slate-950/40 px-4 py-3 text-sm outline-none ring-primary/60 placeholder:text-slate-500 focus:ring-2" />
          <Button type="submit" onClick={() => trackEvent('cta_final_demo')}>{t.cta}</Button>
        </form>
      </div>
    </Section>
  );
}
