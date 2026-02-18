import type { ReactNode } from 'react';

type SectionProps = {
  id?: string;
  title: string;
  eyebrow?: string;
  children: ReactNode;
};

export function Section({ id, title, eyebrow, children }: SectionProps) {
  return (
    <section id={id} className="mx-auto w-full max-w-6xl px-6 py-8 md:py-10">
      <div className="section-shell">
        <header className="mb-5 max-w-4xl">
          {eyebrow ? <p className="mb-3 text-xs uppercase tracking-[0.2em] text-primary">{eyebrow}</p> : null}
          <h2 className="text-3xl font-semibold leading-[1.06] tracking-tight md:text-4xl lg:text-5xl">{title}</h2>
        </header>
        {children}
      </div>
    </section>
  );
}
