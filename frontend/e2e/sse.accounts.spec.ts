import { expect, test, type Page } from './coverage'

async function signIn(page: Page, role: 'operator' | 'reviewer') {
  await page.getByLabel('Account name').fill(`browser.${role}`)
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', {
    name: /Sign in|Switch named account/,
  }).click()
  await expect(page.getByText(`Signed in: browser.${role} (${role})`))
    .toBeVisible()
  await expect(page.getByLabel('Implementation')).toBeEnabled()
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
  await page.getByLabel('Implementation').selectOption('racy')
  await page.getByLabel('Checkpoint release order').selectOption('overlap')
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

test('bursty SSE transitions never overlap authoritative refresh requests', async ({
  page,
}) => {
  test.setTimeout(90_000)
  // Keep genuine EventSource transport; only expose the created stream so
  // a burst of advisory notifications can be tested deterministically.
  await page.addInitScript(() => {
    const NativeSource = window.EventSource
    const streams: EventSource[] = []
    ;(window as typeof window & { __testStreams?: EventSource[] })
      .__testStreams = streams
    window.EventSource = class extends NativeSource {
      constructor(url: string | URL, options?: EventSourceInit) {
        super(url, options)
        streams.push(this)
      }
    }
  })
  await page.goto('/')
  await signIn(page, 'operator')
  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()
  await signIn(page, 'reviewer')
  await page.getByRole('button', { name: /Approve exact hash/ }).click()
  await expect(page.getByText('RULE STATUS: APPROVED')).toBeVisible()
  await signIn(page, 'operator')

  const admission = page.waitForResponse((response) =>
    response.url().endsWith('/api/runs') && response.request().method() === 'POST'
  )
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  const run = (await (await admission).json()) as { run_id: string }
  await expect(page.locator('.run-status')).toHaveText('COMPLETE', {
    timeout: 20_000,
  })
  await expect.poll(() => page.evaluate(() =>
    (window as typeof window & { __testStreams?: EventSource[] })
      .__testStreams?.length ?? 0,
  )).toBeGreaterThan(0)

  let inFlight = 0
  let peak = 0
  let requests = 0
  await page.route(`**/api/runs/${run.run_id}`, async (route) => {
    inFlight += 1
    requests += 1
    peak = Math.max(peak, inFlight)
    await new Promise((resolve) => setTimeout(resolve, 250))
    inFlight -= 1
    await route.continue()
  })
  const notify = () => page.evaluate(() => {
    const streams = (window as typeof window & { __testStreams?: EventSource[] })
      .__testStreams ?? []
    for (const stream of streams) stream.dispatchEvent(new Event('transition'))
  })
  await notify()
  await expect.poll(() => requests).toBeGreaterThan(0)
  for (let i = 0; i < 24; i++) await notify()
  await expect.poll(() => requests >= 2 && inFlight === 0, {
    timeout: 10_000,
  }).toBe(true)
  expect(peak).toBe(1)
  expect(requests).toBeLessThan(5)
  await expect(page.locator('.run-status')).toHaveText('COMPLETE')
  const terminal = page.locator('.transition-list li strong', {
    hasText: 'COMPLETE',
  })
  await expect(terminal).toBeVisible()

  // Deliberately corrupt authoritative pages after an intact prefix has
  // already been accepted. Rehashing/cursor changes cannot rewrite history.
  let corruption: 'prefix' | 'cursor' | 'none' = 'prefix'
  let corruptedPages = 0
  await page.route(
    (url) => url.pathname === `/api/runs/${run.run_id}/events`,
    async (route) => {
      if (corruption === 'none') {
        await route.continue()
        return
      }
      const response = await route.fetch()
      const original = (await response.json()) as {
        events: { version: number; status: string }[]
        next_after: number
      }
      const payload = corruption === 'prefix'
        ? {
            ...original,
            events: original.events.map((event, index) =>
              index === 0 ? { ...event, status: 'FORGED' } : event,
            ),
          }
        : { ...original, next_after: original.next_after + 1 }
      corruptedPages++
      await route.fulfill({
        response, contentType: 'application/json',
        body: JSON.stringify(payload),
      })
    },
  )
  await notify()
  await expect.poll(() => corruptedPages).toBeGreaterThan(0)
  await expect(page.getByText(
    'Cannot refresh the authoritative run snapshot.',
  )).toBeVisible()
  await expect(terminal).toBeVisible()
  await expect(page.locator('.transition-list li strong', {
    hasText: 'FORGED',
  })).toHaveCount(0)

  const firstCorruptCount = corruptedPages
  corruption = 'cursor'
  await notify()
  await expect.poll(() => corruptedPages).toBeGreaterThan(firstCorruptCount)
  await expect(page.getByText(
    'Cannot refresh the authoritative run snapshot.',
  )).toBeVisible()
  await expect(terminal).toBeVisible()
  corruption = 'none'
  await notify()
  await expect(page.getByText(
    'Cannot refresh the authoritative run snapshot.',
  )).toHaveCount(0, { timeout: 12_000 })
  await expect(terminal).toBeVisible()
})

