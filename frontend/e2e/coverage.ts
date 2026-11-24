import { mkdir, writeFile } from 'node:fs/promises'
import { test as base } from '@playwright/test'

export { expect, type Page } from '@playwright/test'

const output = new URL('../coverage/browser/', import.meta.url)

export const test = base.extend<{ sourceCoverage: void }>({
  sourceCoverage: [async ({ page }, use, info) => {
    await use()
    if (process.env.CLASHBOLT_BROWSER_COVERAGE !== '1' || info.status !== 'passed') return
    const counters = await page.evaluate(() => Reflect.get(window, '__coverage__'))
    if (!counters) throw new Error('The browser did not collect source coverage')
    await mkdir(output, { recursive: true })
    await writeFile(new URL(`${info.testId}-${info.retry}.json`, output), JSON.stringify(counters))
  }, { auto: true }],
})
