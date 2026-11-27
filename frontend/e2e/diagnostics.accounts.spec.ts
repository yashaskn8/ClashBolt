import { expect, test } from './coverage'

test('operator sees queue diagnostics but viewer does not', async ({ page }) => {
  await page.route('**/api/operations', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        responsive_workers: 0,
        runs: { PENDING: 3 },
        verifications: { PENDING: 1 },
        target_reset: { sample_count: 1, reset_p95_ms: 42.5 },
        resources: {
          project_id: 'local',
          open_runs: 3,
          max_open: 20,
          open_reserved_cpu_ms: 9000,
          max_reserved_cpu_ms: 180000,
          runs_7d: {
            overspent_count: 0,
            uncertain_count: 0,
            measured_worker_cpu_ms_total: null,
            measured_app_wall_ms_total: null,
            measured_db_active_ms_total: null,
          },
        },
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
  await expect(page.getByLabel('Operational alert delivery status')).toContainText(
    'Alert delivery: in-app polling only · no external notifications sent.',
  )
  await expect(page.getByLabel('Measured resources and cancellation latency')).toContainText(
    'Target reset P95: 42.5 ms (1)',
  )

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
  // The first two responses are healthy; only the third is rejected.
  await expect.poll(() => requests, { timeout: 20_000 }).toBeGreaterThan(2)
  await expect(alert).toHaveCount(0)
  await expect(page.getByText(
    'Operator diagnostics unavailable; current state is unconfirmed.',
  )).toBeVisible()
  // Recoverable operations presentation: clicking Retry refresh recovers when healthy.
  requests = 0
  await page.getByRole('button', { name: 'Retry refresh' }).click()
  await expect(alert).toBeVisible({ timeout: 10_000 })
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
          status: 'UNKNOWN', verdict: 'UNKNOWN',
          execution_backend: 'memory', version: 3,
          cancel_requested: false, step_budget: 4,
          consumed_steps: 2,
          witness: {
            target_id: 'synthetic-inventory',
            target_revision: 'inventory-demo-v1',
            variant: 'locked',
            fixture: { initial_stock: 1, clients: ['a', 'b'], quantity_each: 1 },
            fixture_hash: '0'.repeat(64),
            schedule: 'serial',
            requested_gates: ['a.read', 'a.commit', 'b.read', 'b.commit'],
            observed_steps: [
              { sequence: 1, client: 'a', checkpoint: 'read', observed_stock: 1 },
              { sequence: 2, client: 'a', checkpoint: 'commit', accepted: true, stock_after: 0, confirmed_after: 1 },
            ],
            oracle: { id: 'confirmed-orders-not-above-stock', version: '1', sha256: '1'.repeat(64), authority: 'fixed-demo-rule' },
            final_state: { initial_stock: 1, remaining_stock: 0, confirmed_count: 1 },
            complete: false,
            verdict: 'UNKNOWN',
            evidence_sha256: '2'.repeat(64),
            scope: 'SYNTHETIC_ONLY',
          },
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
  await page.locator('.recent-list button').first().click()
  await expect(page.locator('.trace-gap')).toContainText('b.read, b.commit')
  await expect(page.locator('.trace-gap')).toContainText('omitted events; causal order is not inferred.')
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
  await expect(page.locator('.recent-runs')).toBeVisible()
  await expect(page.locator('.recent-list button')).toHaveCount(0)
})


test('live operator workspace displays project budget, resource accounting, and cancellation latency', async ({
  page,
}) => {
  test.setTimeout(90_000)
  await page.goto('/')
  await page.getByLabel('Account name').fill('browser.operator')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Signed in: browser.operator (operator)')).toBeVisible()
  await expect(page.getByLabel('Project resource budget')).toBeVisible({ timeout: 10_000 })

  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()
  await page.getByLabel('Account name').fill('browser.reviewer')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Switch named account' }).click()
  await expect(page.getByText('Signed in: browser.reviewer (reviewer)')).toBeVisible()
  await page.getByRole('button', { name: /Approve exact hash/ }).click()
  await expect(page.getByText('RULE STATUS: APPROVED')).toBeVisible()

  await page.getByLabel('Account name').fill('browser.operator')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Switch named account' }).click()
  await expect(page.getByText('Signed in: browser.operator (operator)')).toBeVisible()
  await expect(page.getByLabel('Run resource preview')).toContainText(
    'Historical CPU preview: UNAVAILABLE. Provider cost unavailable.',
  )

  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect(page.locator('.run-status')).toHaveText('COMPLETE', { timeout: 20_000 })
  await expect(page.getByLabel('Run resource preview')).toContainText(
    'Measured preview (LOW_SAMPLE_SIZE',
  )
  await expect(page.getByLabel('Run resource preview')).toContainText('Provider cost unavailable.')
  const accounting = page.getByLabel('Run resource accounting')
  await expect(accounting).toBeVisible()
  await expect(accounting).toContainText('Reserved 3000 ms CPU')
  await expect(accounting).toContainText('Provider cost: unavailable')
  await expect(page.getByLabel('Measured resources and cancellation latency')).toContainText(
    'Worker CPU',
  )
  await expect(page.getByLabel('Measured resources and cancellation latency')).toContainText(
    'Target reset P95: no samples',
  )
})
