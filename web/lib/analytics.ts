export type AnalyticsEvent =
  | 'cta_request_demo'
  | 'cta_see_how'
  | 'cta_final_demo'
  | 'scroll_depth_50';

const sentDepthEvents = new Set<string>();

export function trackEvent(event: AnalyticsEvent, payload?: Record<string, string>) {
  if (typeof window === 'undefined') {
    return;
  }

  const data = { event, ...payload };
  window.dispatchEvent(new CustomEvent('synchatica-analytics', { detail: data }));
}

export function trackScrollDepth() {
  if (typeof window === 'undefined') {
    return;
  }

  const onScroll = () => {
    const body = document.body;
    const viewport = window.innerHeight;
    const percent = ((window.scrollY + viewport) / body.scrollHeight) * 100;

    if (percent >= 50 && !sentDepthEvents.has('50')) {
      sentDepthEvents.add('50');
      trackEvent('scroll_depth_50');
    }
  };

  window.addEventListener('scroll', onScroll, { passive: true });
  onScroll();

  return () => window.removeEventListener('scroll', onScroll);
}
