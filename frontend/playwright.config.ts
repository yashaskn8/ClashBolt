import { defineConfig, devices } from '@playwright/test'

const staticSite = process.env.CLASHBOLT_BROWSER_MODE === 'static'

export default defineConfig({
  testDir: './e2e',
  testMatch: process.env.CLASHBOLT_BROWSER_MODE === 'accounts'
    ? '**/*.accounts.spec.ts' : '**/*.spec.ts',
  testIgnore: process.env.CLASHBOLT_BROWSER_MODE === 'accounts'
    ? [] : ['**/*.accounts.spec.ts'],
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  use: {
    baseURL: staticSite ? 'http://127.0.0.1:8000' : 'http://127.0.0.1:5173',
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      command: 'python -m uvicorn clashbolt.api:app --host 127.0.0.1 --port 8000',
      cwd: '..',
      url: 'http://127.0.0.1:8000/api/health',
      env: staticSite ? {
        ...process.env, CLASHBOLT_FRONTEND_DIST: 'frontend/dist',
        CLASHBOLT_ACCESS_MODE: 'local',
      } : process.env,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    ...staticSite ? [] : [{
      command: 'npm run dev',
      url: 'http://127.0.0.1:5173',
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    }],
  ],
})
