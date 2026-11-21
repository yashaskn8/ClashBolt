import { expect, test, type Page } from '@playwright/test'

async function signIn(page: Page, role: 'operator' | 'reviewer' | 'viewer') {
  await page.getByLabel('Account name').fill(`browser.${role}`)
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: /Sign in|Switch named account/ }).click()
  await expect(page.getByText(`Signed in: browser.${role} (${role})`)).toBeVisible()
  await expect(page.getByLabel('Workspace project')).toBeVisible()
  await expect(page.getByLabel('Implementation')).toBeEnabled()
}

async function enterProject(page: Page, project: string) {
  await page.getByLabel('Workspace project').selectOption(project)
  await expect(page.getByLabel('Workspace project')).toHaveValue(project)
  await expect(page.getByLabel('Implementation')).toBeEnabled()
}

test('real browser switches isolated projects without carrying stale rule or drafts', async ({
  page,
}) => {
  test.setTimeout(90_000)
  await page.goto('/')
  await signIn(page, 'operator')
  await expect(page.getByRole('status', { name: 'Run resource preview' }))
    .toContainText('active run slots')

  await page.getByLabel('Implementation').selectOption('locked')
  await page.getByRole('button', { name: /Save experiment draft/ }).click()
  await expect(page.getByText(
    'Draft saved to PostgreSQL. Rule approval is not saved as authority.',
  )).toBeVisible()
  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()

  await enterProject(page, 'browser-workspace')
  await expect(page.getByLabel('Implementation')).toHaveValue('racy')
  await expect(page.getByRole('status', { name: 'Run resource preview' }))
    .toContainText('No current approved oracle for this project')
  const tenantInitial = await page.request.get('/api/oracle/proposals')
  expect(tenantInitial.ok()).toBe(true)
  expect(((await tenantInitial.json()) as { proposals: unknown[] }).proposals).toEqual([])
  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()
  const proposals = await page.request.get('/api/oracle/proposals')
  expect(((await proposals.json()) as { proposals: unknown[] }).proposals).toHaveLength(1)

  await signIn(page, 'reviewer')
  await enterProject(page, 'browser-workspace')
  await page.getByRole('button', { name: /Approve exact hash/ }).click()
  await expect(page.getByText('RULE STATUS: APPROVED')).toBeVisible()
  await expect(page.getByRole('button', { name: /Validate candidate rule/ })).toBeDisabled()

  await signIn(page, 'operator')
  await enterProject(page, 'browser-workspace')
  await expect(page.getByRole('status', { name: 'Run resource preview' }))
    .toContainText('No known blockers now; submission still rechecks')
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect(page.locator('.run-status')).toHaveText('COMPLETE', {
    timeout: 20_000,
  })
  const runInTenant = await page.request.get('/api/runs')
  expect(runInTenant.ok()).toBe(true)
  const tenantIds = (await runInTenant.json()) as { runs: { run_id: string }[] }
  expect(tenantIds.runs.length).toBeGreaterThan(0)
  const tenantRunId = tenantIds.runs[0].run_id

  await enterProject(page, 'local')
  await expect(page.getByLabel('Implementation')).toHaveValue('locked')
  const isolated = await page.request.get(`/api/runs/${tenantRunId}`)
  expect(isolated.status()).toBe(404)
  await expect(page.locator('.run-status')).toHaveCount(0)

  await enterProject(page, 'browser-workspace')
  await expect(page.getByLabel('Implementation')).toHaveValue('racy')
  const restored = await page.request.get(`/api/runs/${tenantRunId}`)
  expect(restored.status()).toBe(200)
})
