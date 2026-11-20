import { expect, test, type Page } from '@playwright/test'

async function signIn(page: Page, role: string) {
  await page.getByLabel('Account name').fill(`browser.${role}`)
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', {
    name: /Sign in|Switch named account/,
  }).click()
  await expect(page.getByText(`Signed in: browser.${role} (${role})`)).toBeVisible()
  await expect(page.getByLabel('Implementation')).toBeEnabled()
}

test('independent reviewer approval permits a durable synthetic run', async ({
  page,
}) => {
  await page.goto('/')
  await expect(page.getByLabel('Account name')).toBeVisible()
  await signIn(page, 'operator')
  await page.getByRole('button', { name: /Inspect model obligations/ }).click()
  await expect(page.getByLabel('Last operation result')).toContainText('"calls_unknown":')
  await expect(page.getByRole('button', {
    name: /Validate candidate rule/,
  })).toBeEnabled()
  await page.getByRole('button', { name: /Validate candidate rule/ }).click()
  await expect(page.getByText('RULE STATUS: PROPOSED')).toBeVisible()

  await signIn(page, 'reviewer')
  await expect(page.getByRole('button', {
    name: /Inspect model obligations/,
  })).toBeDisabled()
  await expect(page.getByRole('button', { name: /Validate candidate rule/ })).toBeDisabled()
  await page.getByRole('button', { name: /Approve exact hash/ }).click()
  await expect(page.getByText('RULE STATUS: APPROVED')).toBeVisible()
  await expect(page.getByRole('button', { name: /Issue one-call capability/ }))
    .toBeDisabled()

  await signIn(page, 'operator')
  await page.getByLabel('Implementation').selectOption('racy')
  await page.getByLabel('Checkpoint release order').selectOption('overlap')
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect(page.locator('.run-status')).toHaveText('COMPLETE', {
    timeout: 15_000,
  })
  await expect(page.getByText('The approved synthetic oracle found two accepted orders for one item.'))
    .toBeVisible()

  await signIn(page, 'viewer')
  await expect(page.getByRole('button', { name: /Submit synthetic experiment/ }))
    .toBeDisabled()
})


test('native database verification is performed by the separate worker', async ({
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
  await page.getByLabel('Target isolation').selectOption('native-postgresql')
  await page.getByRole('button', { name: /Submit synthetic experiment/ }).click()
  await expect(page.locator('.run-status')).toHaveText('COMPLETE', {
    timeout: 20_000,
  })
  await expect(page.getByText('Disposable PostgreSQL target')).toBeVisible()
  await page.getByRole('button', { name: /Replay on a clean fixture/ }).click()
  await expect(page.getByText('REPRODUCED')).toBeVisible({ timeout: 25_000 })
  await page.getByRole('button', { name: /Minimize witness gates/ }).click()
  await expect(page.getByText('MINIMAL')).toBeVisible({ timeout: 25_000 })
  await expect(page.getByText('VERIFIED TRIALS')).toBeVisible()
})
