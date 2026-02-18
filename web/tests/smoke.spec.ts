import { expect, test } from '@playwright/test';

test('landing smoke flow', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { level: 1 })).toContainText('Synchatica turns Telegram and API channels');
  await page.getByRole('button', { name: 'See how it works' }).click();
  await page.mouse.wheel(0, 1800);
  await expect(page.getByText('KPI Impact')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Request demo' }).first()).toBeVisible();
});
