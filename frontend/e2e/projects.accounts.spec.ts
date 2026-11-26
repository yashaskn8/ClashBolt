import { expect, test, type Page } from './coverage'

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
  await page.route('**/api/runs/preview', async (route) => {
    const response = await route.fetch()
    await route.fulfill({ response, json: { ...await response.json(), project_id: 'local' } })
  })
  await page.getByLabel('Implementation').selectOption('locked')
  await expect(page.getByRole('status', { name: 'Run resource preview' }))
    .toContainText('Preview unavailable')
  await page.unroute('**/api/runs/preview')
  await page.getByLabel('Implementation').selectOption('racy')
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

  // Let approval commit in the workspace, then delay delivery until the browser
  // has switched to the local operator context.
  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()
  await signIn(page, 'reviewer')
  await enterProject(page, 'browser-workspace')
  const approvalPattern = '**/api/oracle/proposals/*/approve'
  let deliverApproval: () => void = () => {}
  const delivery = new Promise<void>((resolve) => { deliverApproval = resolve })
  let approvedRule = ''
  await page.route(approvalPattern, async (route) => {
    const response = await route.fetch()
    expect(response.ok()).toBe(true)
    approvedRule = new URL(route.request().url()).pathname.split('/').at(-2) ?? ''
    await delivery
    await route.fulfill({ response })
  })
  await page.getByRole('button', { name: /Approve exact hash/ }).click()
  await expect.poll(() => approvedRule).not.toBe('')
  await signIn(page, 'operator')
  await enterProject(page, 'local')
  const approvalResponse = page.waitForResponse(approvalPattern)
  deliverApproval()
  expect(await (await approvalResponse).finished()).toBe(null)
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
  }))
  const preview = page.waitForRequest((request) => request.url().endsWith('/api/runs/preview'))
  await page.getByLabel('Implementation').selectOption('racy')
  const submitted = (await preview).postDataJSON() as { approved_oracle_id: string | null }
  expect(submitted.approved_oracle_id).not.toBe(approvedRule)

  await enterProject(page, 'browser-workspace')
  await expect(page.getByRole('status', { name: 'Run resource preview' }))
    .toContainText('No known blockers now; submission still rechecks')
  let deliverWorkspaceResponses: () => void = () => {}
  const pendingDelivery = new Promise<void>((resolve) => { deliverWorkspaceResponses = resolve })
  const committed = new Set<string>()
  const delayedPosts = /\/api\/(experiment-draft|runs)$/
  await page.route(delayedPosts, async (route) => {
    if (route.request().method() !== 'POST') {
      await route.continue()
      return
    }
    const response = await route.fetch()
    expect(response.ok()).toBe(true)
    committed.add(new URL(route.request().url()).pathname)
    await pendingDelivery
    await route.fulfill({ response })
  })
  await page.getByRole('button', { name: /Save experiment draft/ }).click()
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect.poll(() => committed.size).toBe(2)
  await enterProject(page, 'local')
  await expect(page.getByText('Recovered saved experiment draft.')).toBeVisible()
  const received = ['/api/experiment-draft', '/api/runs'].map((path) =>
    page.waitForResponse((response) =>
      new URL(response.url()).pathname === path && response.request().method() === 'POST',
    ),
  )
  deliverWorkspaceResponses()
  for (const response of await Promise.all(received)) expect(await response.finished()).toBeNull()
  await page.evaluate(() => new Promise<void>((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
  }))
  await expect(page.locator('.run-status')).toHaveCount(0)
  await expect(page.getByText('Recovered saved experiment draft.')).toBeVisible()
})
