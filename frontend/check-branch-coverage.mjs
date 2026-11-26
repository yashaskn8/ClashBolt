import { readFileSync, readdirSync } from 'node:fs'
import { normalize, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import coverage from 'istanbul-lib-coverage'

const root = fileURLToPath(new URL('.', import.meta.url))
const directory = resolve(root, 'coverage/browser')
const reports = readdirSync(directory).filter((name) => name.endsWith('.json'))
if (reports.length < 15) {
  throw new Error(`Expected coverage from at least 15 browser tests, received ${reports.length}`)
}
const map = coverage.createCoverageMap({})
for (const report of reports) {
  const entries = JSON.parse(readFileSync(resolve(directory, report), 'utf8'))
  for (const entry of Object.values(entries)) {
    map.addFileCoverage({ ...entry, path: normalize(entry.path) })
  }
}

const source = resolve(root, 'src')
const expected = readdirSync(source, { recursive: true })
  .filter((name) => /\.tsx?$/.test(name) && !name.endsWith('.d.ts'))
  .map((name) => resolve(source, name))
  .sort()
const measured = map.files().sort()
if (JSON.stringify(expected) !== JSON.stringify(measured)) {
  throw new Error('Browser coverage must include every TypeScript source module')
}

const { covered, total } = map.getCoverageSummary().branches
const percent = 100 * covered / total
if (!Number.isFinite(percent) || percent < 73) {
  throw new Error(`Frontend branch coverage below 73% floor (${covered}/${total})`)
}
console.log(`Frontend branch coverage: ${covered}/${total} (${percent.toFixed(2)}%)`)
