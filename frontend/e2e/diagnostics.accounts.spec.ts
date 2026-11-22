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
  await expect(page.locator('.recent-runs')).toHaveAttribute('data-operator-ready', 'false')
  await expect(page.getByText(
    'Operator check: Pending experiments have no recently responsive worker.',
  )).toHaveCount(0)
})

test('operator diagnostics fail closed on a rejected refresh', async ({ page }) => {
  test.setTimeout(45_000)
  let requests = 0
  await page.route('**/api/operations', async (route) => {
    requests++
    if (requests > 2) {
      await route.fulfill({ status: 403, body: 'Revoked' })
      return
    }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        responsive_workers: 0, runs: { PENDING: 1 },
        verifications: { PENDING: 0 },
        diagnostics: {
          status: 'ATTENTION',
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
  const alert = page.getByText(
    'Operator check: Pending experiments have no recently responsive worker.',
  )
  await expect(alert).toBeVisible()
  await expect.poll(() => requests, { timeout: 12_000 }).toBeGreaterThan(1)
  await expect(alert).toHaveCount(0)
  await expect(page.getByText(
    'Operator diagnostics unavailable; current state is unconfirmed.',
  )).toBeVisible()
})

test('a failed run-index refresh removes the previously selectable run', async ({ page }) => {
  test.setTimeout(45_000)
  let requests = 0
  await page.route(
    (url) => url.pathname === '/api/runs' && url.searchParams.get('limit') === '10',
    async (route) => {
    requests++
    if (requests > 2) {
      await route.fulfill({ status: 503, body: 'Database unavailable' })
      return
    }
    await route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({
        runs: [{
          run_id: '00000000-0000-4000-8000-000000001234',
          variant: 'locked', schedule: 'serial',
          status: 'COMPLETE', verdict: 'PASS',
          execution_backend: 'memory', version: 3,
          cancel_requested: false, step_budget: 4,
          consumed_steps: 4, witness: null,
        }],
      }),
    })
    },
  )
  await page.goto('/')
  await page.getByLabel('Account name').fill('browser.operator')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Signed in: browser.operator (operator)')).toBeVisible()
  await expect.poll(() => requests, { timeout: 12_000 }).toBeGreaterThan(0)
  await expect(page.locator('.recent-list button')).toHaveCount(1)
  await expect.poll(() => requests, { timeout: 12_000 }).toBeGreaterThan(2)
  await expect(page.locator('.recent-list button')).toHaveCount(0)
  await expect(page.getByText(
    'Recent runs unavailable; check PostgreSQL and the API.',
  )).toBeVisible()
})

test('stale operator diagnostics do not survive account replacement', async ({ page }) => {
  test.setTimeout(45_000)
  let requests = 0
  let release = () => {}
  const blocked = new Promise<void>((resolve) => { release = resolve })
  const stale = {
    responsive_workers: 0, runs: { PENDING: 1 },
    verifications: { PENDING: 0 },
    diagnostics: {
      status: 'ATTENTION',
      alerts: [{
        code: 'NO_RESPONSIVE_WORKER',
        message: 'Pending experiments have no recently responsive worker.',
      }],
    },
  }
  await page.route('**/api/operations', async (route) => {
    requests++
    if (requests > 2) await blocked
    try {
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify(stale),
      })
    } catch {
      // The earlier account's pending HTTP request was aborted.
    }
  })
  await page.goto('/')
  await page.getByLabel('Account name').fill('browser.operator')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Sign in' }).click()
  const oldAlert = page.getByText(
    'Operator check: Pending experiments have no recently responsive worker.',
  )
  await expect(oldAlert).toBeVisible()
  await expect.poll(() => requests, { timeout: 12_000 }).toBeGreaterThan(2)
  await page.getByLabel('Account name').fill('browser.viewer')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Switch named account' }).click()
  await expect(page.getByText('Signed in: browser.viewer (viewer)')).toBeVisible()
  await expect(page.locator('.recent-runs')).toHaveAttribute('data-operator-ready', 'false')
  await expect(oldAlert).toHaveCount(0)
  release()
  await page.waitForTimeout(300)
  await expect(oldAlert).toHaveCount(0)
  await expect(page.locator('.recent-list')).toBeVisible()
})

