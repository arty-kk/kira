import type { Metadata } from 'next';

export const siteUrl = 'https://synchatica.ai';

export const baseMetadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: 'Synchatica — продвинутые чат-персоны на базе AI и Ematory.',
  description:
    'Synchatica создает уникальных чат-персон, бренд-аватаров, и комьюнити-менеджеров на базе AI и Ematory. Они говорят естественно, помнят пользователей, модерируют сообщество и ведут к целевым действиям 24/7.',
  openGraph: {
    title: 'Synchatica — продвинутые чат-персоны на базе AI и Ematory.',
    description:
      'Synchatica создает уникальных чат-персон, бренд-аватаров, и комьюнити-менеджеров на базе AI и Ematory. Они говорят естественно, помнят пользователей, модерируют сообщество и ведут к целевым действиям 24/7.',
    url: siteUrl,
    siteName: 'Synchatica',
    type: 'website'
  },
  alternates: {
    canonical: '/'
  }
};

export const organizationSchema = {
  '@context': 'https://schema.org',
  '@type': 'Organization',
  name: 'Synchatica',
  url: siteUrl,
  description:
    'Synchatica создаёт чат-персон на базе AI и Ematory для Telegram и социальных платформ: память, эмоциональная адаптация, модерация и рост вовлеченности 24/7.'
};
