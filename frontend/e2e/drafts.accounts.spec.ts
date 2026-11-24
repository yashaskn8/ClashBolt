import { expect, test, type Page } from './coverage'

async function signIn(page: Page, name: string) {
  await page.getByLabel('Account name').fill(name)
  await page.getByLabel('Account password').fill('browser-local-password-000')
  await page.getByRole('button', {
    name: /Sign in|Switch named account/,
  }).click()
  await expect(page.getByText(`Signed in: ${name} (operator)`)).toBeVisible()
  await expect(page.getByLabel('Implementation')).toBeEnabled()
  await expect(page.getByText(
    /No saved draft for this account|Recovered saved experiment draft/,
  )).toBeVisible()
}

test('two operator accounts keep separate versioned experiment drafts', async ({
  page,
}) => {
  await page.goto('/')
  await signIn(page, 'browser.operator')
  await page.getByLabel('Implementation').selectOption('locked')
  await page.getByRole('button', { name: /Save experiment draft/ }).click()
  await expect(page.getByText('Draft saved to PostgreSQL. Rule approval is not saved as authority.'))
    .toBeVisible()

  await signIn(page, 'browser.second')
  // A new account never sees the previous operator's form values.
  await expect(page.getByLabel('Implementation')).toHaveValue('racy')
  await page.getByRole('button', { name: /Save experiment draft/ }).click()
  await expect(page.getByText('Draft saved to PostgreSQL. Rule approval is not saved as authority.'))
    .toBeVisible()

  await signIn(page, 'browser.operator')
  await expect(page.getByLabel('Implementation')).toHaveValue('locked')
  await expect(page.getByText('Recovered saved experiment draft.')).toBeVisible()
})
