'use client';

import { motion, useReducedMotion } from 'motion/react';

export function AmbientBackground() {
  const reducedMotion = useReducedMotion();

  if (reducedMotion) {
    return null;
  }

  return (
    <div className="pointer-events-none fixed inset-0 -z-10 overflow-hidden" aria-hidden>
      <motion.div
        animate={{ x: [0, 90, 0], y: [0, -40, 0] }}
        transition={{ duration: 22, repeat: Infinity, ease: 'easeInOut' }}
        className="absolute -left-20 top-[22%] h-72 w-72 rounded-full bg-cyan-400/10 blur-3xl"
      />
      <motion.div
        animate={{ x: [0, -80, 0], y: [0, 60, 0] }}
        transition={{ duration: 26, repeat: Infinity, ease: 'easeInOut' }}
        className="absolute right-[-120px] top-[12%] h-[360px] w-[360px] rounded-full bg-indigo-500/10 blur-3xl"
      />
      <motion.div
        animate={{ x: [0, 45, 0], y: [0, 55, 0] }}
        transition={{ duration: 24, repeat: Infinity, ease: 'easeInOut' }}
        className="absolute bottom-[-180px] left-[30%] h-[420px] w-[420px] rounded-full bg-primary/10 blur-3xl"
      />
    </div>
  );
}
