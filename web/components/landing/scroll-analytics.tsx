'use client';

import { useEffect } from 'react';
import { trackScrollDepth } from '@/lib/analytics';

export function ScrollAnalytics() {
  useEffect(() => {
    const cleanup = trackScrollDepth();
    return cleanup;
  }, []);

  return null;
}
