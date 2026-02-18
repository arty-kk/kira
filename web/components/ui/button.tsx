'use client';

import type { MouseEventHandler, ReactNode } from 'react';
import { motion } from 'motion/react';

type ButtonProps = {
  children: ReactNode;
  variant?: 'primary' | 'ghost';
  className?: string;
  type?: 'button' | 'submit' | 'reset';
  onClick?: MouseEventHandler<HTMLButtonElement>;
};

export function Button({ children, variant = 'primary', className = '', type = 'button', onClick }: ButtonProps) {
  const base =
    'rounded-lg px-5 py-3 text-sm font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary';
  const variantClass =
    variant === 'primary'
      ? 'bg-primary text-slate-950 hover:bg-accent'
      : 'border border-slate-700 bg-transparent text-text hover:border-primary hover:text-primary';

  return (
    <motion.button
      type={type}
      onClick={onClick}
      whileHover={{ y: -2 }}
      transition={{ duration: 0.2 }}
      className={`${base} ${variantClass} ${className}`}
    >
      {children}
    </motion.button>
  );
}
