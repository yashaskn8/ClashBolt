import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: process.env.CLASHBOLT_BROWSER_MODE === 'accounts'
    ? '**/*.accounts.spec.ts' : '**/*.spec.ts',
  testIgnore: process.env.CLASHBOLT_BROWSER_MODE === 'accounts'
    ? [] : ['**/*.accounts.spec.ts'],
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: 'http://127.0.0.1:5173',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: 'python -m uvicorn clashbolt.api:app --host 127.0.0.1 --port 8000',
      cwd: '..',
      url: 'http://127.0.0.1:8000/api/health',
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: 'npm run dev',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
  ],
})
