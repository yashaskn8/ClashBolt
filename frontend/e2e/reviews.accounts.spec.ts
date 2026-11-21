import { createHash } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'

async function signIn(page: Page, role: 'operator' | 'reviewer') {
  await page.getByLabel('Account name').fill(`browser.${role}`)
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: /Sign in|Switch named account/ }).click()
  await expect(page.getByText(`Signed in: browser.${role} (${role})`))
    .toBeVisible()
}

test('reviewer records only worker-reproduced synthetic evidence', async ({ page }) => {
  test.setTimeout(100_000)
  await page.goto('/')
  await signIn(page, 'operator')
  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()

  await signIn(page, 'reviewer')
  await page.getByRole('button', { name: /Approve exact hash/ }).click()
  await expect(page.getByText('RULE STATUS: APPROVED')).toBeVisible()

  await signIn(page, 'operator')
  // Another account-mode browser test persists a locked/serial draft. Never
  // inherit its PASS fixture: only a reproduced FAIL can be supported here.
  await page.getByLabel('Implementation').selectOption('racy')
  await page.getByLabel('Checkpoint release order').selectOption('overlap')
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect(page.locator('.run-status')).toHaveText('COMPLETE', {
    timeout: 20_000,
  })
  await expect(page.locator('.verdict-fail')).toBeVisible()
  const runs = await page.request.get('/api/runs')
  expect(runs.ok()).toBe(true)
  const runId = ((await runs.json()) as { runs: { run_id: string }[] }).runs[0].run_id

  const [bundle] = await Promise.all([
    page.waitForEvent('download'),
    page.getByRole('button', { name: 'Export verifier bundle (ZIP)' }).click(),
  ])
  expect(bundle.suggestedFilename()).toBe(`clashbolt-witness-${runId}.zip`)
  const actualSha = createHash('sha256')
    .update(await readFile(await bundle.path())).digest('hex')
  const exportsResponse = await page.request.get(`/api/runs/${runId}/exports`)
  expect(exportsResponse.ok()).toBe(true)
  const exported = await exportsResponse.json() as {
    exports: { artifact_sha256: string; current: boolean }[]
  }
  expect(exported.exports).toHaveLength(1)
  expect(exported.exports[0].current).toBe(true)
  expect(exported.exports[0].artifact_sha256).toBe(actualSha)
  await expect(page.getByLabel('Audited evidence export')).toContainText(actualSha)

  const queued = await page.request.post(`/api/runs/${runId}/verification-jobs`, {
    data: { kind: 'REPLAY' },
  })
  expect(queued.status()).toBe(202)
  const jobId = ((await queued.json()) as { job_id: string }).job_id
  await expect.poll(async () => {
    const response = await page.request.get(`/api/verification-jobs/${jobId}`)
    return ((await response.json()) as { status: string }).status
  }, { timeout: 25_000 }).toBe('COMPLETE')
  const workerReplay = await page.request.get(`/api/verification-jobs/${jobId}`)
  const replayRecord = await workerReplay.json() as {
    status: string
    result: { status: string; original_sha256?: string }
  }
  expect(replayRecord.result.status).toBe('REPRODUCED')
  const readiness = await page.request.get(`/api/runs/${runId}/reviews`)
  expect(readiness.status()).toBe(200)
  const initialReview = await readiness.json() as {
    replay_verified: boolean; replay_job_id: string | null
  }
  expect(initialReview, 'Server must bind a completed worker replay').toMatchObject({
    replay_verified: true, replay_job_id: jobId,
  })

  await signIn(page, 'reviewer')
  await page.locator('.recent-list button').filter({
    hasText: runId.slice(0, 13),
  }).click()
  const panel = page.getByRole('region', { name: 'Independent synthetic witness review' })
  await expect(panel.getByLabel('Synthetic replay readiness'))
    .toContainText('VERIFIED SYNTHETIC REPLAY')
  await expect(panel.getByLabel('Synthetic replay readiness'))
    .toContainText('External reproduction: NOT ESTABLISHED')
  await panel.getByLabel('Your review reason').fill(
    'This exact synthetic replay was inspected against the bound witness.',
  )
  await panel.getByRole('button', { name: 'Support verified synthetic replay' }).click()
  await expect(panel.getByText('SUPPORTED · CURRENT', { exact: false })).toBeVisible()
  const reviewResult = await page.request.get(`/api/runs/${runId}/reviews`)
  expect(reviewResult.ok()).toBe(true)
  const data = await reviewResult.json() as { reviews: { decision: string }[] }
  expect(data.reviews).toHaveLength(1)
  expect(data.reviews[0].decision).toBe('SUPPORTED')
})
