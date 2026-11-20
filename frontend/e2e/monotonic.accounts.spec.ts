"""This fixture is exercised against a real browser and native PostgreSQL API."""
import { expect, test, type Page } from '@playwright/test'

async function signIn(page: Page, role: 'operator' | 'reviewer') {
  await page.getByLabel('Account name').fill(`browser.${role}`)
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: /Sign in|Switch named account/ }).click()
  await expect(page.getByText(`Signed in: browser.${role} (${role})`))
    .toBeVisible()
  await expect(page.getByLabel('Implementation')).toBeEnabled()
}

test('late older snapshots cannot rewind a completed durable run', async ({
  page,
}) => {
  test.setTimeout(90_000)
  await page.goto('/')
  await signIn(page, 'operator')
  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()

  await signIn(page, 'reviewer')
  await page.getByRole('button', { name: /Approve exact hash/ }).click()
  await expect(page.getByText('RULE STATUS: APPROVED')).toBeVisible()

  await signIn(page, 'operator')
  await page.getByLabel('Implementation').selectOption('racy')
  await page.getByLabel('Checkpoint release order').selectOption('overlap')
  const submission = page.waitForResponse((response) =>
    response.url().endsWith('/api/runs') && response.request().method() === 'POST'
  )
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  const run = (await (await submission).json()) as { run_id: string }
  await expect(page.locator('.run-status')).toHaveText('COMPLETE', {
    timeout: 20_000,
  })

  let sentStale = false
  await page.route(`**/api/runs/${run.run_id}`, async (route) => {
    if (sentStale) {
      await route.continue()
      return
    }
    const real = await route.fetch()
    const snapshot = (await real.json()) as Record<string, unknown>
    // Simulate a delayed, obsolete poll response despite newer DB state.
    await route.fulfill({
      response: real,
      contentType: 'application/json',
      body: JSON.stringify({
        ...snapshot, status: 'PENDING', version: 0, verdict: null, witness: null,
      }),
    })
    sentStale = true
  })
  await expect.poll(() => sentStale, { timeout: 10_000 }).toBe(true)
  await page.waitForTimeout(300)
  await expect(page.locator('.run-status')).toHaveText('COMPLETE')
  await expect(page.locator('.transition-list li strong', {
    hasText: 'COMPLETE',
  })).toBeVisible()
})
