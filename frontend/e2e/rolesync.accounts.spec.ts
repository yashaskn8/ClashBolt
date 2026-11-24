import { execFileSync } from 'node:child_process'
import { expect, test } from './coverage'

function setRole(role: 'viewer' | 'operator' | null) {
  execFileSync(
    'python',
    [
      '-m', 'clashbolt.accounts',
      role === null ? 'revoke-local' : 'grant-local',
      'browser.rolesync',
      ...(role === null ? [] : [role]),
    ],
    { cwd: '..', env: process.env },
  )
}

test('browser reconciles a live operator downgrade and project revocation', async ({
  page,
}) => {
  test.setTimeout(45_000)
  await page.goto('/')
  await page.getByLabel('Account name').fill('browser.rolesync')
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByText('Signed in: browser.rolesync (operator)'))
    .toBeVisible()
  await expect(page.getByRole('button', { name: /Validate candidate rule/ }))
    .toBeEnabled()

  try {
    setRole('viewer')
    await expect(page.getByText('Signed in: browser.rolesync (viewer)'))
      .toBeVisible({ timeout: 15_000 })
    await expect(page.getByRole('button', { name: /Validate candidate rule/ }))
      .toBeDisabled()
    await expect(page.getByRole('button', { name: /Submit synthetic experiment/ }))
      .toBeDisabled()

    // This uses the browser's cookie and the real API, not just UI state.
    const denied = await page.request.post('/api/oracle/proposals', { data: {} })
    expect(denied.status()).toBe(403)

    setRole(null)
    await expect(page.getByText('Account or project access changed. Sign in again.'))
      .toBeVisible({ timeout: 15_000 })
    await expect(page.getByText('Sign in with a locally provisioned account.'))
      .toBeVisible()
    await expect(page.getByRole('button', { name: /Validate candidate rule/ }))
      .toHaveCount(0)
  } finally {
    // Restore for independent reruns, even if an assertion fails.
    setRole('operator')
  }
})
