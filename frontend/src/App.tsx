import { useEffect, useState } from 'react'
import DurableMonitor, { type DurableRun } from './DurableMonitor'
import PolicyConsole from './PolicyConsole'
import RecentRuns from './RecentRuns'

type Variant = 'racy' | 'locked'
type Schedule = 'overlap' | 'serial'
type ExecutionBackend = 'memory' | 'native-postgresql'
type Verdict = 'PASS' | 'FAIL' | 'UNKNOWN'
type Role = 'viewer' | 'operator' | 'reviewer'
type AccountIdentity = { username: string; role: Role; project_id: string }
type ProjectGrant = { id: string; role: Role }

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
type Reduction = {
  status: string
  original_gate_count: number
  minimal_gate_count: number
  report_sha256: string
  reduction_trials: { removed_gate: string; verdict: Verdict; accepted: boolean }[]
}
type Catalog = {
  target_id: string
  target_revision: string
  storage: 'postgresql' | 'memory-only'
  access_mode: 'local' | 'private' | 'accounts'
  execution_backends: ExecutionBackend[]
  oracle: { id: string; version: string; sha256: string; rule: string; authority: string }
}

type RunPreview = {
  project_id: string
  step_budget: number
  open_runs: number
  max_open: number
  preflight_ok: boolean
  blockers: string[]
  reservation_made: boolean
  resource_bound: {
    checkpoint_limit: number
    disposable_databases: number
    cpu_seconds_estimate: number | null
  }
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
  const [runPreview, setRunPreview] = useState<RunPreview | null>(null)
  const [previewUnavailable, setPreviewUnavailable] = useState(false)
  const [approvedOracleId, setApprovedOracleId] = useState<string | null>(null)
  const [draftVersion, setDraftVersion] = useState(0)
  const [draftReady, setDraftReady] = useState(true)
  const [draftMessage, setDraftMessage] = useState('')
  const [operatorToken, setOperatorToken] = useState('')
  const [sessionReady, setSessionReady] = useState(false)
  const [accountName, setAccountName] = useState('')
  const [accountPassword, setAccountPassword] = useState('')
  const [account, setAccount] = useState<AccountIdentity | null>(null)
  const [accountProjects, setAccountProjects] = useState<ProjectGrant[]>([])
  const [result, setResult] = useState<Experiment | null>(null)
  const [durable, setDurable] = useState<DurableRun | null>(null)
  const [replayStatus, setReplayStatus] = useState('NOT_RUN')
  const [reduction, setReduction] = useState<Reduction | null>(null)
  const [reducing, setReducing] = useState(false)
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
    if (
      catalog?.storage !== 'postgresql' ||
      (catalog.access_mode === 'accounts' && account?.role !== 'operator') ||
      (catalog.access_mode !== 'accounts' && !operatorToken) ||
      (catalog.access_mode === 'private' && !sessionReady)
    ) {
      setRunPreview(null)
      setPreviewUnavailable(false)
      return
    }
    let cancelled = false
    setRunPreview(null)
    setPreviewUnavailable(false)
    fetch('/api/runs/preview', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(catalog.access_mode !== 'accounts'
          ? { 'X-Operator-Token': operatorToken } : {}),
      },
      body: JSON.stringify({
        target_id: 'synthetic-inventory',
        variant,
        schedule,
        step_budget: 4,
        approved_oracle_id: approvedOracleId,
        execution_backend: executionBackend,
      }),
    })
      .then((response) => parseResponse<RunPreview>(response))
      .then((data) => { if (!cancelled) setRunPreview(data) })
      .catch(() => { if (!cancelled) setPreviewUnavailable(true) })
    return () => { cancelled = true }
  }, [
    catalog?.storage, catalog?.access_mode, account?.role, account?.project_id,
    operatorToken, sessionReady, approvedOracleId, variant, schedule, executionBackend,
  ])

  useEffect(() => {
    if (catalog?.access_mode !== 'accounts') return
    fetch('/api/auth/me')
      .then((response) => parseResponse<AccountIdentity>(response))
      .then(setAccount)
      .catch(() => setAccount(null))
  }, [catalog?.access_mode])

  useEffect(() => {
    if (catalog?.access_mode !== 'accounts' || !account?.username) return
    const username = account.username
    const selectedProject = account.project_id
    let mounted = true

    async function refreshProjectRole() {
      try {
        const response = await fetch('/api/auth/me', { cache: 'no-store' })
        if (!mounted) return
        if (response.status === 401 || response.status === 403) {
          // A revoked project grant or account session makes previous
          // operator controls and evidence selections obsolete immediately
          // after the authoritative HTTP response, not after a new login.
          setAccount(null)
          setApprovedOracleId(null)
          setDurable(null)
          setResult(null)
          setDraftVersion(0)
          setDraftReady(false)
          setError('Account or project access changed. Sign in again.')
          return
        }
        if (!response.ok) return // transient outage: backend still rejects writes
        const identity = (await response.json()) as AccountIdentity
        if (!mounted || identity.username !== username) return
        if (identity.project_id !== selectedProject) {
          setDurable(null)
          setResult(null)
          setApprovedOracleId(null)
          setDraftVersion(0)
          setDraftReady(false)
        }
        setAccount((current) => (
          current?.username === username &&
          (current.role !== identity.role || current.project_id !== identity.project_id)
            ? identity : current
        ))
      } catch {
        // Never infer permission from a failed network probe. All writes
        // are independently authorized against the current database role.
      }
    }

    const timer = window.setInterval(() => void refreshProjectRole(), 2000)
    return () => { mounted = false; window.clearInterval(timer) }
  }, [catalog?.access_mode, account?.username, account?.project_id])

  useEffect(() => {
    if (catalog?.access_mode !== 'accounts' || !account) {
      setAccountProjects([])
      return
    }
    let cancelled = false
    fetch('/api/auth/projects', { cache: 'no-store' })
      .then((response) => parseResponse<{ projects: ProjectGrant[] }>(response))
      .then((payload) => {
        if (!cancelled) setAccountProjects(payload.projects)
      })
      .catch(() => {
        if (!cancelled) setAccountProjects([])
      })
    return () => { cancelled = true }
  }, [catalog?.access_mode, account?.username, account?.project_id, account?.role])

  useEffect(() => {
    if (catalog?.storage !== 'postgresql') return
    if (catalog.access_mode === 'accounts' && !account) return
    if (catalog.access_mode === 'private' && !sessionReady) return
    let cancelled = false
    fetch('/api/oracle/proposals')
      .then((response) => parseResponse<{ proposals: { id: string; state: string }[] }>(response))
      .then((data) => {
        if (!cancelled) {
          setApprovedOracleId(
            data.proposals.find((rule) => rule.state === 'APPROVED')?.id ?? null
          )
        }
      })
      .catch(() => { if (!cancelled) setApprovedOracleId(null) })
    return () => { cancelled = true }
  }, [catalog?.storage, catalog?.access_mode, sessionReady, account?.username, account?.project_id])

  useEffect(() => {
    if (catalog?.storage !== 'postgresql') return
    if (catalog.access_mode === 'private' && !sessionReady) return
    if (catalog.access_mode === 'accounts' && !account) return
    let cancelled = false
    setDraftReady(false)
    fetch('/api/experiment-draft')
      .then((response) => parseResponse<{
        saved: {
          version: number
          draft: {
            variant: Variant
            schedule: Schedule
            execution_backend: ExecutionBackend
          }
        } | null
      }>(response))
      .then((response) => {
        if (cancelled) return
        if (!response.saved) {
          setDraftVersion(0)
          if (catalog.access_mode === 'accounts') {
            setDraftMessage('No saved draft for this account.')
          }
          return
        }
        setDraftVersion(response.saved.version)
        setVariant(response.saved.draft.variant)
        setSchedule(response.saved.draft.schedule)
        if (catalog.execution_backends.includes(response.saved.draft.execution_backend)) {
          setExecutionBackend(response.saved.draft.execution_backend)
        }
        setDraftMessage('Recovered saved experiment draft.')
      })
      .catch(() => {
        if (!cancelled) setDraftMessage('Saved draft unavailable; current form is unchanged.')
      })
      .finally(() => {
        if (!cancelled) setDraftReady(true)
      })
    return () => { cancelled = true }
  }, [catalog?.storage, catalog?.access_mode, sessionReady, account?.username, account?.project_id])

  async function saveDraft() {
    setDraftMessage('')
    try {
      const response = await fetch('/api/experiment-draft', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(catalog?.access_mode === 'accounts'
            ? {} : { 'X-Operator-Token': operatorToken }),
        },
        body: JSON.stringify({
          target_id: 'synthetic-inventory',
          expected_version: draftVersion,
          variant, schedule, execution_backend: executionBackend,
        }),
      })
      const saved = await parseResponse<{ version: number }>(response)
      setDraftVersion(saved.version)
      setDraftMessage('Draft saved to PostgreSQL. Rule approval is not saved as authority.')
    } catch {
      setDraftMessage('Draft update was rejected; refresh before overwriting changes.')
    }
  }

  async function loginAccount() {
    setError('')
    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: accountName,
          password: accountPassword,
        }),
      })
      const identity = await parseResponse<AccountIdentity>(response)
      setDraftReady(false)
      setAccountProjects([])
      setAccount(identity)
      setAccountPassword('')
      setDraftVersion(0)
      setDraftMessage('')
      setVariant('racy')
      setSchedule('overlap')
      setExecutionBackend('memory')
      setApprovedOracleId(null)
      setDurable(null)
      setResult(null)
    } catch {
      setError('Account login was rejected. Check credentials or account lockout.')
    }
  }

  async function selectAccountProject(projectId: string) {
    if (!account || account.project_id === projectId) return
    setError('')
    setDraftReady(false)
    try {
      const response = await fetch('/api/auth/select-project', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId }),
      })
      const identity = await parseResponse<AccountIdentity>(response)
      setApprovedOracleId(null)
      setDurable(null)
      setResult(null)
      setDraftVersion(0)
      setDraftMessage('')
      setVariant('racy')
      setSchedule('overlap')
      setExecutionBackend('memory')
      setReplayStatus('NOT_RUN')
      setReduction(null)
      setAccount(identity)
    } catch {
      setError('Project selection was denied. Refresh the current project grants.')
      setDraftReady(true)
    }
  }

  async function logoutAccount() {
    try {
      await fetch('/api/auth/logout', { method: 'POST' })
    } finally {
      setAccount(null)
      setAccountProjects([])
      setDraftReady(false)
      setDraftVersion(0)
      setDraftMessage('')
      setVariant('racy')
      setSchedule('overlap')
      setExecutionBackend('memory')
      setApprovedOracleId(null)
      setDurable(null)
      setResult(null)
    }
  }

  async function unlock() {
    setError('')
    try {
      const response = await fetch('/api/session', {
        method: 'POST',
        headers: { 'X-Operator-Token': operatorToken },
      })
      await parseResponse<{ status: string }>(response)
      setSessionReady(true)
    } catch (cause) {
      setSessionReady(false)
      setError(cause instanceof Error ? cause.message : 'Operator login failed')
    }
  }

  async function run() {
    setBusy(true)
    setError('')
    setResult(null)
    setDurable(null)
    setReplayStatus('NOT_RUN')
    setReduction(null)
    try {
      const storage = catalog?.storage === 'postgresql' ? 'runs' : 'experiments'
      const response = await fetch(`/api/${storage}`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(catalog?.access_mode !== 'accounts' &&
            (storage === 'runs' || catalog?.access_mode === 'private')
              ? { 'X-Operator-Token': operatorToken } : {}),
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

  async function verifyOnWorker(kind: 'REPLAY' | 'REDUCE') {
    if (!durable) throw new Error('No durable witness to verify')
    const response = await fetch(
      `/api/runs/${durable.run_id}/verification-jobs`,
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(catalog?.access_mode === 'accounts'
            ? {} : { 'X-Operator-Token': operatorToken }),
        },
        body: JSON.stringify({ kind }),
      },
    )
    const submitted = await parseResponse<{ job_id: string }>(response)
    for (let attempt = 0; attempt < 40; attempt++) {
      const poll = await fetch(`/api/verification-jobs/${submitted.job_id}`)
      const job = await parseResponse<{
        status: 'PENDING' | 'RUNNING' | 'COMPLETE' | 'UNKNOWN'
        result: (Reduction & { status: string }) | null
      }>(poll)
      if (job.status === 'COMPLETE' && job.result) return job.result
      if (job.status === 'UNKNOWN') {
        throw new Error(job.result?.status ?? 'Verification evidence is uncertain')
      }
      await new Promise((resolve) => setTimeout(resolve, 750))
    }
    throw new Error('Verification is queued. Check the worker and retry later.')
  }

  async function replay() {
    if (!result && !durable?.witness) return
    setReplaying(true)
    setError('')
    try {
      if (durable?.execution_backend === 'native-postgresql') {
        setReplayStatus('QUEUED')
        const report = await verifyOnWorker('REPLAY')
        setReplayStatus(report.status === 'REPRODUCED' ? 'REPRODUCED' : 'UNCONFIRMED')
      } else {
        const path = durable ? `runs/${durable.run_id}` : `experiments/${result!.run_id}`
        const response = await fetch(`/api/${path}/replay`, {
          method: 'POST',
          headers: durable && catalog?.access_mode !== 'accounts'
            ? { 'X-Operator-Token': operatorToken } : {},
        })
        const proof = await parseResponse<{ replay_status: string }>(response)
        setReplayStatus(proof.replay_status)
        if (result) setResult({ ...result, replay_status: proof.replay_status })
      }
    } catch (cause) {
      setReplayStatus('UNCONFIRMED')
      setError(cause instanceof Error ? cause.message : 'Unexpected replay error')
    } finally {
      setReplaying(false)
    }
  }

  async function minimize() {
    if (!durable || durable.verdict !== 'FAIL') return
    setReducing(true)
    setError('')
    try {
      if (durable.execution_backend === 'native-postgresql') {
        const report = await verifyOnWorker('REDUCE')
        if (report.status !== 'MINIMAL' && report.status !== 'BOUNDED') {
          throw new Error('Worker could not verify the minimized evidence')
        }
        setReduction(report)
      } else {
        const response = await fetch(`/api/runs/${durable.run_id}/reduce`, {
          method: 'POST',
          headers: catalog?.access_mode === 'accounts'
            ? {} : { 'X-Operator-Token': operatorToken },
        })
        setReduction(await parseResponse<Reduction>(response))
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Witness reduction failed')
    } finally {
      setReducing(false)
    }
  }

  const operatorReady = catalog?.access_mode === 'accounts'
    ? account?.role === 'operator'
    : !!operatorToken
  const accountUnlocked = catalog?.access_mode !== 'accounts' || account !== null
  const witness = durable?.witness ?? result?.witness
  const adjudicatedVerdict = durable ? durable.verdict ?? 'UNKNOWN' : witness?.verdict

  return (
    <div className="site-shell">
      <aside className="sidebar" aria-label="Project navigation">
        <div className="logo"><span className="logo-mark">⚡</span><span>Clash<span className="logo-accent">Bolt</span></span></div>
        <div className="sidebar-caption">CONCURRENCY INTELLIGENCE</div>
        <nav className="navigation" aria-label="Workspace">
          <span className="nav-active">◈ &nbsp; Experiment lab</span>
          {durable ? (
            <a className="nav-link" href="#run-timeline">◷ &nbsp; Run timeline</a>
          ) : (
            <span className="nav-disabled">
              ◷ &nbsp; Run timeline <em>Select a run</em>
            </span>
          )}
          {catalog?.storage === 'postgresql' && (
            catalog.access_mode === 'local' ||
            (catalog.access_mode === 'private' && sessionReady) ||
            (catalog.access_mode === 'accounts' && account !== null)
          ) ? (
            <a className="nav-link" href="#agent-planning">▣ &nbsp; Agent planning</a>
          ) : (
            <span className="nav-disabled">
              ▣ &nbsp; Agent planning <em>Unlock workspace</em>
            </span>
          )}
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
            <select id="variant" value={variant} disabled={!draftReady}
              onChange={(event) => setVariant(event.target.value as Variant)}>
              <option value="racy">Racy — checks stale stock before commit</option>
              <option value="locked">Corrected — rechecks stock at commit</option>
            </select>

            <label className="field-label" htmlFor="schedule">Checkpoint release order</label>
            <select id="schedule" value={schedule} disabled={!draftReady}
              onChange={(event) => setSchedule(event.target.value as Schedule)}>
              <option value="overlap">Overlap — A.read → B.read → A.commit → B.commit</option>
              <option value="serial">Serial — A.read → A.commit → B.read → B.commit</option>
            </select>
            <p className="schedule-tip">Application checkpoints only. The controller does not control operating-system or database internals.</p>
            {catalog?.storage === 'postgresql' && (
              <>
                <label className="field-label" htmlFor="execution-backend">Target isolation</label>
                <select
                  id="execution-backend" value={executionBackend}
                  disabled={!draftReady}
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
            {(catalog?.storage === 'postgresql' || catalog?.access_mode === 'private') &&
             catalog?.access_mode !== 'accounts' && (
              <>
              <label className="field-label" htmlFor="operator-token">Local operator token</label>
              <input
                id="operator-token" type="password" autoComplete="off"
                value={operatorToken} onChange={(event) => {
                  setOperatorToken(event.target.value)
                  setSessionReady(false)
                }}
              />
              {catalog?.access_mode === 'private' && (
                <button
                  className="secondary-button" type="button"
                  disabled={!operatorToken || sessionReady} onClick={unlock}
                >
                  {sessionReady ? 'Private session unlocked' : 'Unlock private API'}
                </button>
              )}
              {catalog?.storage === 'postgresql' && (
                <p className="schedule-tip">
                Approved oracle: {approvedOracleId
                  ? approvedOracleId.slice(0, 13) + '…'
                  : 'none — approve the fixed rule in the console below before submitting'}
                </p>
              )}
              </>
            )}
            {catalog?.access_mode === 'accounts' && (
              <section aria-label="Named account access">
                <p className="help-text">
                  {account ? `Signed in: ${account.username} (${account.role})`
                    : 'Sign in with a locally provisioned account.'}
                </p>
                {account && accountProjects.length > 1 && (
                  <>
                    <label className="field-label" htmlFor="project-selector">
                      Workspace project
                    </label>
                    <select
                      id="project-selector"
                      value={account.project_id}
                      onChange={(event) => void selectAccountProject(event.target.value)}
                    >
                      {accountProjects.map((project) => (
                        <option key={project.id} value={project.id}>
                          {project.id} ({project.role})
                        </option>
                      ))}
                    </select>
                  </>
                )}
                <label className="field-label" htmlFor="account-name">Account name</label>
                <input
                  id="account-name" autoComplete="username"
                  value={accountName}
                  onChange={(event) => setAccountName(event.target.value)}
                />
                <label className="field-label" htmlFor="account-password">
                  Account password
                </label>
                <input
                  id="account-password" type="password" autoComplete="current-password"
                  value={accountPassword}
                  onChange={(event) => setAccountPassword(event.target.value)}
                />
                <button className="secondary-button" type="button"
                  onClick={() => void loginAccount()}
                  disabled={!accountName || !accountPassword}>
                  {account ? 'Switch named account' : 'Sign in'}
                </button>
                {account && (
                  <button className="secondary-button" type="button"
                    onClick={() => void logoutAccount()}>Sign out</button>
                )}
              </section>
            )}
            {catalog?.storage === 'postgresql' && (
              <>
                <button
                  className="secondary-button" type="button" onClick={saveDraft}
                  disabled={!draftReady || !operatorReady || (catalog.access_mode === 'private' && !sessionReady)}
                >
                  Save experiment draft
                </button>
                {draftMessage && <p className="help-text">{draftMessage}</p>}
              </>
            )}
            {catalog?.storage === 'postgresql' && (
              <div className="rule-info" role="status" aria-label="Run resource preview">
                <span>RESOURCE &amp; ADMISSION PREVIEW / NOT A RESERVATION</span>
                {runPreview && (
                  <>
                    <strong>
                      {runPreview.open_runs}/{runPreview.max_open} active run slots ·
                      {runPreview.resource_bound.checkpoint_limit} checkpoint budget
                    </strong>
                    <small>
                      {runPreview.resource_bound.disposable_databases === 1
                        ? 'One disposable native database requested. '
                        : 'Fresh in-memory fixture requested. '}
                      CPU/provider cost is not measured.
                    </small>
                    <small>
                      {runPreview.preflight_ok
                        ? 'No known blockers now; submission still rechecks all permissions and quotas.'
                        : runPreview.blockers.join('; ')}
                    </small>
                  </>
                )}
                {!runPreview && (
                  <small>
                    {previewUnavailable
                      ? 'Preview unavailable; submission will still be validated by the server.'
                      : 'Unlock an operator session to check current quota and approval.'}
                  </small>
                )}
              </div>
            )}
            <button
              className="primary-button" type="button" onClick={run}
              disabled={
                busy || !catalog || !draftReady ||
                (catalog.access_mode === 'private' && !sessionReady) ||
                !accountUnlocked ||
                (catalog.access_mode === 'accounts' && !operatorReady) ||
                (catalog.storage === 'postgresql' && (!approvedOracleId || !operatorReady))
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
                {durable?.verdict === 'FAIL' && (
                  <button className="secondary-button" type="button"
                    onClick={minimize} disabled={reducing || !operatorReady}>
                    {reducing ? 'Checking candidate deletions…' : 'Minimize witness gates'}
                  </button>
                )}
                {reduction && (
                  <div className="evidence">
                    <div><span>REDUCTION</span><strong>{reduction.status}</strong></div>
                    <div><span>CONTROLLED GATES</span>
                      <strong>{reduction.original_gate_count} → {reduction.minimal_gate_count}</strong>
                    </div>
                    <div><span>VERIFIED TRIALS</span>
                      <strong>{reduction.reduction_trials.length}</strong>
                    </div>
                    <div><span>REPORT SHA256</span>
                      <code title={reduction.report_sha256}>
                        {shortHash(reduction.report_sha256)}
                      </code>
                    </div>
                  </div>
                )}
                <p className="help-text">Replay compares the complete deterministic evidence hash. It does not establish uncontrolled reproduction.</p>
              </div>
            )}
          </section>
        </div>
        {catalog?.storage === 'postgresql' &&
        (catalog.access_mode === 'local' ||
          (catalog.access_mode === 'private' && sessionReady) ||
          (catalog.access_mode === 'accounts' && account !== null)) && (
          <RecentRuns
            key={catalog.access_mode === 'accounts' ? account?.project_id : 'local'}
            operatorReady={operatorReady}
            accountMode={catalog.access_mode === 'accounts'}
            operatorToken={operatorToken}
            onSelect={(run) => {
            setDurable(run)
            setResult(null)
            setReplayStatus('NOT_RUN')
            setReduction(null)
          }} />
        )}
        {durable && <DurableMonitor
          initial={durable} onUpdate={setDurable} operatorToken={operatorToken}
          canOperate={operatorReady} accountMode={catalog?.access_mode === 'accounts'}
        />}
        {catalog?.storage === 'postgresql' &&
        (catalog.access_mode === 'local' ||
          (catalog.access_mode === 'private' && sessionReady) ||
          (catalog.access_mode === 'accounts' && account !== null)) && <PolicyConsole
          key={catalog.access_mode === 'accounts'
            ? `${account?.username}:${account?.project_id}:${account?.role}` : catalog.access_mode}
          variant={variant} schedule={schedule}
          onApproved={(id) => setApprovedOracleId(id)}
          operator={operatorToken}
          accountRole={catalog.access_mode === 'accounts' ? account?.role : undefined}
        />}
        <footer>CLASHBOLT LAB <span>·</span> Synthetic targets only. PostgreSQL mode keeps durable control state, not external effects.</footer>
      </main>
    </div>
  )
}
