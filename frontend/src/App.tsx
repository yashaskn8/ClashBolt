import { useEffect, useState } from 'react'
import DurableMonitor, { type DurableRun } from './DurableMonitor'
import PolicyConsole from './PolicyConsole'
import RecentRuns from './RecentRuns'

type Variant = 'racy' | 'locked'
type Schedule = 'overlap' | 'serial'
type ExecutionBackend = 'memory' | 'native-postgresql'
type Verdict = 'PASS' | 'FAIL' | 'UNKNOWN'

type Step = {
  sequence: number
  client: string
  checkpoint: string
  observed_stock?: number
  accepted?: boolean
  stock_after?: number
  confirmed_after?: number
}

type Witness = {
  target_id: string
  target_revision: string
  variant: Variant
  fixture: { initial_stock: number; clients: string[]; quantity_each: number }
  fixture_hash: string
  schedule: Schedule
  requested_gates: string[]
  observed_steps: Step[]
  oracle: { id: string; version: string; sha256: string; authority: string }
  final_state: { initial_stock: number; remaining_stock: number; confirmed_count: number }
  complete: boolean
  verdict: Verdict
  evidence_sha256: string
  scope: string
}

type Experiment = { run_id: string; witness: Witness; replay_status: string }
type Catalog = {
  target_id: string
  target_revision: string
  storage: 'postgresql' | 'memory-only'
  execution_backends: ExecutionBackend[]
  oracle: { id: string; version: string; sha256: string; rule: string; authority: string }
}

async function parseResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw new Error(`API request failed (${response.status}). Check that the local backend is running.`)
  }
  return (await response.json()) as T
}

const shortHash = (hash: string) => `${hash.slice(0, 12)}…`

export default function App() {
  const [variant, setVariant] = useState<Variant>('racy')
  const [schedule, setSchedule] = useState<Schedule>('overlap')
  const [executionBackend, setExecutionBackend] = useState<ExecutionBackend>('memory')
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  const [approvedOracleId, setApprovedOracleId] = useState<string | null>(null)
  const [operatorToken, setOperatorToken] = useState('')
  const [result, setResult] = useState<Experiment | null>(null)
  const [durable, setDurable] = useState<DurableRun | null>(null)
  const [replayStatus, setReplayStatus] = useState('NOT_RUN')
  const [busy, setBusy] = useState(false)
  const [replaying, setReplaying] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    fetch('/api/catalog')
      .then((response) => parseResponse<Catalog>(response))
      .then(setCatalog)
      .catch(() => setError('Cannot connect to the local API. Start uvicorn on 127.0.0.1:8000.'))
  }, [])

  useEffect(() => {
    if (catalog?.storage !== 'postgresql') return
    fetch('/api/oracle/proposals')
      .then((response) => parseResponse<{ proposals: { id: string; state: string }[] }>(response))
      .then((data) => {
        setApprovedOracleId(data.proposals.find((rule) => rule.state === 'APPROVED')?.id ?? null)
      })
      .catch(() => setApprovedOracleId(null))
  }, [catalog?.storage])

  async function run() {
    setBusy(true)
    setError('')
    setResult(null)
    setDurable(null)
    setReplayStatus('NOT_RUN')
    try {
      const storage = catalog?.storage === 'postgresql' ? 'runs' : 'experiments'
      const response = await fetch(`/api/${storage}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(storage === 'runs' ? { 'X-Operator-Token': operatorToken } : {}),
        },
        body: JSON.stringify({
          target_id: 'synthetic-inventory', variant, schedule,
          ...(storage === 'runs'
            ? {
                client_key: crypto.randomUUID(),
                step_budget: 4,
                approved_oracle_id: approvedOracleId,
                execution_backend: executionBackend,
              }
            : {}),
        }),
      })
      if (storage === 'runs') setDurable(await parseResponse<DurableRun>(response))
      else setResult(await parseResponse<Experiment>(response))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unexpected experiment error')
    } finally {
      setBusy(false)
    }
  }

  async function replay() {
    if (!result && !durable?.witness) return
    setReplaying(true)
    setError('')
    try {
      const path = durable ? `runs/${durable.run_id}` : `experiments/${result!.run_id}`
      const response = await fetch(`/api/${path}/replay`, {
        method: 'POST',
        headers: durable ? { 'X-Operator-Token': operatorToken } : {},
      })
      const proof = await parseResponse<{ replay_status: string }>(response)
      setReplayStatus(proof.replay_status)
      if (result) setResult({ ...result, replay_status: proof.replay_status })
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Unexpected replay error')
    } finally {
      setReplaying(false)
    }
  }

  const witness = durable?.witness ?? result?.witness
  const adjudicatedVerdict = durable ? durable.verdict ?? 'UNKNOWN' : witness?.verdict

  return (
    <div className="site-shell">
      <aside className="sidebar" aria-label="Project navigation">
        <div className="logo"><span className="logo-mark">⚡</span><span>Clash<span className="logo-accent">Bolt</span></span></div>
        <div className="sidebar-caption">CONCURRENCY INTELLIGENCE</div>
        <nav className="navigation" aria-label="Workspace">
          <span className="nav-active">◈ &nbsp; Experiment lab</span>
          <span className="nav-disabled">◷ &nbsp; Run timeline <em>Later</em></span>
          <span className="nav-disabled">▣ &nbsp; Agent planning <em>Later</em></span>
        </nav>
        <div className="sidebar-foot"><span className="live-dot" /> Local synthetic mode <small>Stage 2 · PostgreSQL when configured</small></div>
      </aside>

      <main className="main">
        <header className="topbar"><span>WORKSPACE <span className="crumb">/</span> EXPERIMENT LAB</span><span className="topbar-right">CLASHBOLT <b>0.2</b></span></header>
        <section className="intro">
          <div className="eyebrow">CONTROLLED CONCURRENCY EXPLORATION</div>
          <h1>Find the clash.<br /><span>Prove the behavior.</span></h1>
          <p>Run two concurrent purchases against a disposable synthetic inventory fixture.
          Compare a stale-check bug with a version that revalidates stock at commit.</p>
          <div className="notice">SYNTHETIC DEMONSTRATION — Outcomes below are not findings against a real application.</div>
        </section>

        <div className="columns">
          <section className="panel composer" aria-labelledby="composer-title">
            <div className="panel-kicker">01 / EXPERIMENT COMPOSER</div>
            <h2 id="composer-title">Configure a run</h2>
            <p className="panel-description">The backend allows only this built-in fixture, two implementations, and two bounded schedules.</p>

            <label className="field-label" htmlFor="target">Target fixture</label>
            <input id="target" value={catalog?.target_id ?? 'synthetic-inventory'} readOnly />
            <small className="help-text">Fresh state for every run · Initial stock: 1 · Two clients</small>

            <label className="field-label" htmlFor="variant">Implementation</label>
            <select id="variant" value={variant} onChange={(event) => setVariant(event.target.value as Variant)}>
              <option value="racy">Racy — checks stale stock before commit</option>
              <option value="locked">Corrected — rechecks stock at commit</option>
            </select>

            <label className="field-label" htmlFor="schedule">Checkpoint release order</label>
            <select id="schedule" value={schedule} onChange={(event) => setSchedule(event.target.value as Schedule)}>
              <option value="overlap">Overlap — A.read → B.read → A.commit → B.commit</option>
              <option value="serial">Serial — A.read → A.commit → B.read → B.commit</option>
            </select>
            <p className="schedule-tip">Application checkpoints only. The controller does not control operating-system or database internals.</p>
            {catalog?.storage === 'postgresql' && (
              <>
                <label className="field-label" htmlFor="execution-backend">Target isolation</label>
                <select
                  id="execution-backend" value={executionBackend}
                  onChange={(event) => {
                    setExecutionBackend(event.target.value as ExecutionBackend)
                  }}
                >
                  <option value="memory">Fresh in-memory fixture</option>
                  {catalog.execution_backends.includes('native-postgresql') && (
                    <option value="native-postgresql">
                      Fresh isolated native PostgreSQL database
                    </option>
                  )}
                </select>
                <p className="help-text">
                  Native database mode requires a second local PostgreSQL cluster.
                  Database separation is not an OS or network sandbox.
                </p>
              </>
            )}

            <div className="rule-info"><span>INVARIANT / IMMUTABLE DEMO RULE</span><strong>{catalog?.oracle.rule ?? 'confirmed_count ≤ initial_stock; remaining_stock matches confirmations'}</strong><small>Version {catalog?.oracle.version ?? '1'} · Fixed fixture rule, not human-approved production policy</small></div>
            {catalog?.storage === 'postgresql' && (
              <>
              <label className="field-label" htmlFor="operator-token">Local operator token</label>
              <input
                id="operator-token" type="password" autoComplete="off"
                value={operatorToken} onChange={(event) => setOperatorToken(event.target.value)}
              />
              <p className="schedule-tip">
                Approved oracle: {approvedOracleId
                  ? approvedOracleId.slice(0, 13) + '…'
                  : 'none — approve the fixed rule in the console below before submitting'}
              </p>
              </>
            )}
            <button
              className="primary-button" type="button" onClick={run}
              disabled={
                busy || !catalog ||
                (catalog.storage === 'postgresql' && (!approvedOracleId || !operatorToken))
              }
            >{busy ? 'Running experiment…' : 'Submit synthetic experiment'} <span>→</span></button>
            {error && <p className="error" role="alert">{error}</p>}
            <p className="help-text">Storage: {catalog?.storage ?? 'connecting'} · Durable runs require a separate worker.</p>
          </section>

          <section className="panel witness" aria-labelledby="witness-title">
            <div className="panel-kicker">02 / WITNESS REVIEW</div>
            <h2 id="witness-title">Observed evidence</h2>
            {!witness ? (
              <div className="empty-state"><div className="empty-graphic"><span>A</span><i /> <span>B</span></div><h3>No experiment yet</h3><p>Choose a fixture implementation and schedule, then run to inspect the controlled history.</p></div>
            ) : (
              <div className="witness-content">
                <div className="result-heading"><span className={`verdict verdict-${adjudicatedVerdict?.toLowerCase()}`}>{adjudicatedVerdict}</span><span className="synthetic-tag">SYNTHETIC ONLY</span></div>
                <p className="verdict-explanation">{adjudicatedVerdict === 'FAIL'
                  ? 'The approved synthetic oracle found two accepted orders for one item.'
                  : adjudicatedVerdict === 'PASS'
                    ? 'The approved synthetic rule found no invariant violation.'
                    : 'No current approved verdict. Historical evidence may have been withdrawn.'}</p>
                <div className="metrics"><div><small>INITIAL STOCK</small><strong>{witness.final_state.initial_stock}</strong></div><div><small>CONFIRMED</small><strong>{witness.final_state.confirmed_count}</strong></div><div><small>REMAINING</small><strong>{witness.final_state.remaining_stock}</strong></div></div>
                <h3 className="subhead">Controlled checkpoint history</h3>
                <ol className="steps">{witness.observed_steps.map((step) => <li key={step.sequence}><span className="step-number">{String(step.sequence).padStart(2, '0')}</span><span className="step-client">Client {step.client.toUpperCase()}</span><span className="step-detail">{step.checkpoint === 'read' ? `Read stock = ${step.observed_stock}` : step.accepted ? `Committed · stock → ${step.stock_after}` : 'Rejected · out of stock'}</span></li>)}</ol>
                <div className="evidence"><div><span>FIXTURE SHA256</span><code title={witness.fixture_hash}>{shortHash(witness.fixture_hash)}</code></div><div><span>ORACLE</span><code>{witness.oracle.id} @ {witness.oracle.version}</code></div><div><span>EVIDENCE SHA256</span><code title={witness.evidence_sha256}>{shortHash(witness.evidence_sha256)}</code></div><div><span>REPLAY</span><strong>{replayStatus !== 'NOT_RUN' ? replayStatus : result?.replay_status ?? 'NOT_RUN'}</strong></div></div>
                <button className="secondary-button" type="button" onClick={replay} disabled={replaying}>{replaying ? 'Replaying on fresh state…' : 'Replay on a clean fixture'} ↻</button>
                <p className="help-text">Replay compares the complete deterministic evidence hash. It does not establish uncontrolled reproduction.</p>
              </div>
            )}
          </section>
        </div>
        {catalog?.storage === 'postgresql' && (
          <RecentRuns onSelect={(run) => {
            setDurable(run)
            setResult(null)
            setReplayStatus('NOT_RUN')
          }} />
        )}
        {durable && <DurableMonitor
          initial={durable} onUpdate={setDurable} operatorToken={operatorToken}
        />}
        {catalog?.storage === 'postgresql' && <PolicyConsole
          variant={variant} schedule={schedule}
          onApproved={(id) => setApprovedOracleId(id)}
          operator={operatorToken}
        />}
        <footer>CLASHBOLT LAB <span>·</span> Synthetic targets only. PostgreSQL mode keeps durable control state, not external effects.</footer>
      </main>
    </div>
  )
}
