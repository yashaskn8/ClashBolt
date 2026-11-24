import { createRequire } from 'node:module'
import { defineConfig } from 'vite'
import base from './vite.config'

const { createInstrumenter } = createRequire(import.meta.url)('istanbul-lib-instrument')

export default defineConfig({
  ...base,
  plugins: [
    {
      name: 'clashbolt-branch-coverage',
      enforce: 'pre',
      transform(code, id) {
        if (!/\/frontend\/src\/.*\.tsx?$/.test(id)) return
        const instrumenter = createInstrumenter({
          esModules: true,
          parserPlugins: ['typescript', 'jsx'],
          produceSourceMap: true,
        })
        return { code: instrumenter.instrumentSync(code, id), map: instrumenter.lastSourceMap() }
      },
    },
    ...base.plugins ?? [],
  ],
})
