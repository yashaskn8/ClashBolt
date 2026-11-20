import { expect, test } from '@playwright/test'

test('operator sees queue diagnostics but viewer does not', async ({ page }) => {
  await page.route('**/api/operations', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        responsive_workers: 0,
        runs: { PENDING: 3 },
        verifications: { PENDING: 1 },
        diagnostics: {
          status: 'ATTENTION',
          notifications_sent: false,
          automatic_remediation: false,
          alerts: [{
            code: 'NO_RESPONSIVE_WORKER',
            message: 'Pending experiments have no recently responsive worker.',
          }],
        },
      }),
    })
  })
  await page.goto('/')
  await page.getByLabel('Account name').fill('browser.operator')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Signed in: browser.operator (operator)')).toBeVisible()
  await expect(page.getByText(
    'Operator check: Pending experiments have no recently responsive worker.',
  )).toBeVisible({ timeout: 10_000 })

  await page.getByLabel('Account name').fill('browser.viewer')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Switch named account' }).click()
  await expect(page.getByText('Signed in: browser.viewer (viewer)')).toBeVisible()
  await expect(page.getByText(
    'Operator check: Pending experiments have no recently responsive worker.',
  )).toHaveCount(0)
})
