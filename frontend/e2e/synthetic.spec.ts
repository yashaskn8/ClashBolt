import { expect, test } from '@playwright/test'

test('submits a synthetic failure and replays its witness', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Configure a run' })).toBeVisible()

  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect(page.getByText(
    'The approved synthetic oracle found two accepted orders for one item.',
  )).toBeVisible()
  await expect(page.getByText('SYNTHETIC ONLY')).toBeVisible()

  await page.getByRole('button', { name: /Replay on a clean fixture/ }).click()
  await expect(page.getByText('REPRODUCED')).toBeVisible()
})

test('corrected synthetic fixture passes and arbitrary targets are rejected', async ({
  page,
  request,
}) => {
  await page.goto('/')
  await page.getByLabel('Implementation').selectOption('locked')
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect(page.getByText(
    'The approved synthetic rule found no invariant violation.',
  )).toBeVisible()

  const invalid = await request.post('/api/experiments', {
    data: { variant: 'racy', schedule: 'overlap', url: 'https://example.org' },
  })
  expect(invalid.status()).toBe(422)
})
