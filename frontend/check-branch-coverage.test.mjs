import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { copyFileSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'

const frontend = fileURLToPath(new URL('.', import.meta.url))

function runGate(testContext, scenario) {
  const scratch = join(frontend, 'coverage')
  mkdirSync(scratch, { recursive: true })
  const root = mkdtempSync(join(scratch, 'gate-'))
  testContext.after(() => rmSync(root, { recursive: true, force: true }))
  const gate = join(root, 'check-branch-coverage.mjs')
  const reports = join(root, 'coverage', 'browser')
  const source = join(root, 'src')
  mkdirSync(reports, { recursive: true })
  mkdirSync(source)
  copyFileSync(join(frontend, 'check-branch-coverage.mjs'), gate)
  const filename = join(source, 'example.ts')
  writeFileSync(filename, 'export const example = true\n')
  const location = { start: { line: 1, column: 0 }, end: { line: 1, column: 1 } }
  for (let index = 0; index < (scenario === 'too-few' ? 14 : 15); index++) {
    const alternate = index % 2 === 0
    const path = scenario === 'other-module' ? join(source, 'other.ts')
      : scenario === 'mixed-paths' && alternate ? `${source}/nested/../example.ts` : filename
    const entry = {
      path,
      statementMap: {},
      fnMap: {},
      branchMap: { 0: { type: 'if', loc: location, locations: [location, location] } },
      s: {},
      f: {},
      b: { 0: scenario === 'mixed-paths' && alternate ? [0, 1] : [1, 0] },
    }
    writeFileSync(join(reports, `${index}.json`), JSON.stringify({ [path]: entry }))
  }
  return spawnSync(process.execPath, [gate], { encoding: 'utf8' })
}

test('merges branches reported under equivalent source paths exactly once', (t) => {
  const result = runGate(t, 'mixed-paths')
  assert.equal(result.status, 0, result.stderr)
  assert.match(result.stdout, /Frontend branch coverage: 2\/2 \(100\.00%\)/)
})

test('rejects reports for a different source module', (t) => {
  const result = runGate(t, 'other-module')
  assert.equal(result.status, 1)
  assert.match(result.stderr, /must include every TypeScript source module/)
})

test('rejects too few browser reports', (t) => {
  const result = runGate(t, 'too-few')
  assert.equal(result.status, 1)
  assert.match(result.stderr, /Expected coverage from at least 15 browser tests/)
})

test('continues to enforce the branch coverage floor', (t) => {
  const result = runGate(t, 'below-floor')
  assert.equal(result.status, 1)
  assert.match(result.stderr, /below 73% floor \(1\/2\)/)
})
