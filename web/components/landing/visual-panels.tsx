'use client';

import { useEffect, useState } from 'react';
import { motion, useReducedMotion } from 'motion/react';

export function HeroVisualPanel() {
  const reducedMotion = useReducedMotion();

  return (
    <div className="surface-card relative h-[360px] overflow-hidden rounded-2xl p-5 md:h-[430px]">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_20%_20%,rgba(68,232,197,0.25),transparent_45%),radial-gradient(circle_at_80%_70%,rgba(123,144,255,0.24),transparent_55%)]" />
      <motion.div
        initial={false}
        animate={reducedMotion ? {} : { rotate: 360 }}
        transition={{ duration: 24, repeat: Infinity, ease: 'linear' }}
        className="absolute left-1/2 top-1/2 h-60 w-60 -translate-x-1/2 -translate-y-1/2 rounded-full border border-primary/35"
      />
      <motion.div
        initial={false}
        animate={reducedMotion ? {} : { rotate: -360 }}
        transition={{ duration: 18, repeat: Infinity, ease: 'linear' }}
        className="absolute left-1/2 top-1/2 h-44 w-44 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/25"
      />
      <div className="absolute left-8 top-8 h-36 w-36 rounded-full border border-primary/40" />
      <div className="absolute bottom-10 right-10 h-40 w-40 rounded-full border border-white/15" />
      <div className="absolute inset-x-10 top-1/2 h-px -translate-y-1/2 bg-gradient-to-r from-transparent via-primary/60 to-transparent" />
      <div className="absolute inset-y-10 left-1/2 w-px -translate-x-1/2 bg-gradient-to-b from-transparent via-primary/45 to-transparent" />
      <div className="absolute left-[22%] top-[28%] h-3 w-3 rounded-full bg-primary shadow-glow" />
      <div className="absolute right-[24%] top-[42%] h-2.5 w-2.5 rounded-full bg-sky-300" />
      <div className="absolute bottom-[24%] left-[37%] h-2 w-2 rounded-full bg-violet-300" />
    </div>
  );
}

const snapshots = [
  { tone: 82, empathy: 68, confidence: 74, bars: [28, 46, 64, 52, 34, 61, 73, 59, 40, 49, 66, 57] },
  { tone: 66, empathy: 84, confidence: 70, bars: [48, 35, 57, 69, 51, 43, 62, 78, 65, 41, 54, 72] },
  { tone: 75, empathy: 72, confidence: 86, bars: [34, 58, 45, 61, 77, 56, 43, 67, 81, 62, 46, 70] }
] as const;

export function EmatorySignalGraphic() {
  const reducedMotion = useReducedMotion();
  const [frame, setFrame] = useState(0);
  const current = snapshots[frame];

  useEffect(() => {
    if (reducedMotion) return;
    const timer = window.setInterval(() => setFrame((prev) => (prev + 1) % snapshots.length), 1700);
    return () => window.clearInterval(timer);
  }, [reducedMotion]);

  return (
    <div className="surface-card relative overflow-hidden rounded-xl p-4">
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_30%_25%,rgba(68,232,197,0.24),transparent_45%)]" />

      <div className="relative z-10 mb-3 grid grid-cols-3 gap-2 text-[10px] uppercase tracking-[0.14em]">
        <div className="rounded-md border border-primary/30 bg-slate-950/45 px-2 py-1 text-center text-primary">Tone {current.tone}%</div>
        <div className="rounded-md border border-sky-300/30 bg-slate-950/45 px-2 py-1 text-center text-sky-300">Empathy {current.empathy}%</div>
        <div className="rounded-md border border-violet-300/30 bg-slate-950/45 px-2 py-1 text-center text-violet-300">Confidence {current.confidence}%</div>
      </div>

      <div className="relative z-10 h-24 rounded-lg border border-slate-700/70 bg-slate-950/35 px-3 pb-2 pt-3">
        {[0, 1, 2].map((row) => (
          <div key={row} className="absolute left-3 right-3 h-px bg-white/10" style={{ top: `${24 + row * 23}%` }} />
        ))}

        <div className="relative flex h-full items-end gap-1.5">
          {current.bars.map((value, idx) => (
            <motion.div
              key={`${idx}-${frame}`}
              className="w-full rounded-t-sm bg-gradient-to-t from-primary/45 via-primary/70 to-sky-300/80"
              initial={false}
              animate={{ height: `${value}%` }}
              transition={{ duration: reducedMotion ? 0 : 0.45, ease: 'easeOut' }}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

export function LaunchFlowGraphic() {
  const reducedMotion = useReducedMotion();
  return (
    <div className="surface-card mt-5 overflow-hidden rounded-xl p-5">
      <div className="grid gap-3 md:grid-cols-5">
        {[1, 2, 3, 4, 5].map((step) => (
          <motion.div
            key={step}
            initial={false}
            animate={reducedMotion ? {} : { y: [0, -4, 0] }}
            transition={{ duration: 2 + step * 0.2, repeat: Infinity, ease: 'easeInOut' }}
            className="rounded-lg border border-slate-700/70 bg-slate-950/35 p-3 text-center text-xs"
          >
            <p className="text-primary">STEP {step}</p>
            <div className="mx-auto mt-2 h-1.5 w-16 rounded-full bg-gradient-to-r from-primary/80 to-sky-300/70" />
          </motion.div>
        ))}
      </div>
    </div>
  );
}
