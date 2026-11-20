import { expect, test, type Page } from '@playwright/test'

async function signIn(page: Page, role: 'operator' | 'reviewer') {
  await page.getByLabel('Account name').fill(`browser.${role}`)
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', {
    name: /Sign in|Switch named account/,
  }).click()
  await expect(page.getByText(`Signed in: browser.${role} (${role})`))
    .toBeVisible()
}

test('snapshot and timeline recover when the SSE transport is unavailable', async ({
  page,
}) => {
  test.setTimeout(90_000)
  // An unreliable notification channel must not become the source of truth.
  await page.route('**/api/runs/*/stream*', (route) => route.abort())
  await page.goto('/')
  await signIn(page, 'operator')
  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()

  await signIn(page, 'reviewer')
  await page.getByRole('button', { name: /Approve exact hash/ }).click()
  await expect(page.getByText('RULE STATUS: APPROVED')).toBeVisible()

  await signIn(page, 'operator')
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect(page.locator('.run-status')).toHaveText('COMPLETE', {
    timeout: 20_000,
  })
  await expect(page.locator('.transition-list li strong', {
    hasText: 'COMPLETE',
  })).toBeVisible({ timeout: 12_000 })
  await expect(page.getByText(
    'The approved synthetic oracle found two accepted orders for one item.',
  )).toBeVisible()
})
