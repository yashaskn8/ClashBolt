import { useEffect, useRef, useState } from 'react'
import type { DurableRun } from './DurableMonitor'

type Props = {
  onSelect: (run: DurableRun) => void
  operatorReady: boolean
  accountMode: boolean
  operatorToken: string
  contextKey: string
}
type Operations = {
  responsive_workers: number
  runs: Record<string, number>
  verifications: Record<string, number>
  cancellation?: {
    sample_count: number
    p95_seconds: number | null
    max_seconds: number | null
    in_flight: number
    oldest_in_flight_seconds: number | null
  }
  target_reset?: {
    sample_count: number
    reset_p95_ms: number | null
  }
  resources?: {
    project_id: string
    open_runs: number
    max_open: number
    open_reserved_cpu_ms: number
    max_reserved_cpu_ms: number
    runs_7d: {
      overspent_count: number
      uncertain_count: number
      measured_worker_cpu_ms_total: number | null
      measured_app_wall_ms_total: number | null
      measured_db_active_ms_total: number | null
    }
  }
  diagnostics?: {
    status: 'NORMAL' | 'ATTENTION'
    alerts: { code: string; message: string }[]
  }
}

function formatMs(value: number | null | undefined) {
  return typeof value === 'number' ? `${value} ms` : 'unavailable'
}

export default function RecentRuns({
  onSelect, operatorReady, accountMode, operatorToken, contextKey,
}: Props) {
  const [runs, setRuns] = useState<{ context: string; entries: DurableRun[] }>({
    context: '', entries: [],
  })
  const [operations, setOperations] = useState<{
    context: string; snapshot: Operations
  } | null>(null)
  const [error, setError] = useState('')
  const refreshRef = useRef<() => void>(() => {})

  useEffect(() => {
    let mounted = true
    let refreshing = false
    let refreshAgain = false
    const controller = new AbortController()
    // Role, project, and credential changes invalidate the previous view.
    setRuns({ context: contextKey, entries: [] })
    setOperations(null)
    setError('')

    async function load() {
      try {
        const response = await fetch('/api/runs?limit=10', {
          signal: controller.signal,
          cache: 'no-store',
        })
        if (!response.ok) throw new Error('Run index is not authoritative')
        const parsed = (await response.json()) as { runs: DurableRun[] }
        if (mounted) {
          setRuns({ context: contextKey, entries: parsed.runs })
          setError('')
        }
      } catch {
        if (mounted) {
          // Never continue showing an index after authorization or DB loss.
          setRuns({ context: contextKey, entries: [] })
          setOperations(null)
          setError('Recent runs unavailable; check PostgreSQL and the API.')
        }
        return
      }
      if (!operatorReady) {
        if (mounted) setOperations(null)
        return
      }
      try {
        const response = await fetch('/api/operations', {
          signal: controller.signal,
          cache: 'no-store',
          headers: accountMode ? {} : { 'X-Operator-Token': operatorToken },
        })
        if (!response.ok) throw new Error('Operator diagnostics no longer authorized')
        const status = (await response.json()) as Operations
        if (mounted) setOperations({ context: contextKey, snapshot: status })
      } catch {
        if (mounted) {
          // A rejected or disconnected operator probe is not a healthy reading.
          setOperations(null)
          setError('Operator diagnostics unavailable; current state is unconfirmed.')
        }
      }
    }

    async function refresh() {
      if (!mounted) return
      if (refreshing) {
        refreshAgain = true
        return
      }
      refreshing = true
      try {
        do {
          refreshAgain = false
          await load()
        } while (mounted && refreshAgain)
      } finally {
        refreshing = false
      }
    }
    void refresh()
    refreshRef.current = () => void refresh()
    const timer = setInterval(() => void refresh(), 5000)
    return () => {
      mounted = false
      refreshRef.current = () => {}
      clearInterval(timer)
      controller.abort()
    }
  }, [operatorReady, accountMode, operatorToken, contextKey])

  const currentRuns = runs.context === contextKey ? runs.entries : []
  const currentOperations = operatorReady && operations?.context === contextKey
    ? operations.snapshot : null

  return (
    <section className="panel recent-runs" aria-labelledby="recent-title"
      data-operator-ready={operatorReady ? 'true' : 'false'}>
      <div className="panel-kicker">03 / RECOVERY INDEX</div>
      <h2 id="recent-title">Reopen a durable run</h2>
      <p className="panel-description">
        Select a PostgreSQL run after reloading the page or restarting the coordinator.
        This is a local-only workspace.
      </p>
      {currentOperations && (
        <p className="help-text">
          Live workers: {currentOperations.responsive_workers} ·
          Pending runs: {currentOperations.runs.PENDING ?? 0} ·
          Verification queue: {currentOperations.verifications.PENDING ?? 0}
        </p>
      )}
      {currentOperations?.resources && (
        <>
          <p className="help-text" aria-label="Project resource budget">
            Project budget ({currentOperations.resources.project_id}):{' '}
            {currentOperations.resources.open_runs}/{currentOperations.resources.max_open} runs ·{' '}
            {currentOperations.resources.open_reserved_cpu_ms}/
            {currentOperations.resources.max_reserved_cpu_ms} ms CPU reserved ·{' '}
            Overspent (7d): {currentOperations.resources.runs_7d.overspent_count} ·{' '}
            Uncertain: {currentOperations.resources.runs_7d.uncertain_count} ·{' '}
            Provider cost: unavailable
          </p>
          <p className="help-text" aria-label="Measured resources and cancellation latency">
            Measured (7d): Worker CPU {formatMs(currentOperations.resources.runs_7d.measured_worker_cpu_ms_total)} ·{' '}
            App wall {formatMs(currentOperations.resources.runs_7d.measured_app_wall_ms_total)} ·{' '}
            DB active {formatMs(currentOperations.resources.runs_7d.measured_db_active_ms_total)} ·{' '}
            Target reset P95:{' '}
            {typeof currentOperations.target_reset?.reset_p95_ms === 'number'
              ? `${currentOperations.target_reset.reset_p95_ms} ms (${currentOperations.target_reset.sample_count})`
              : 'no samples'} ·{' '}
            Cancel P95:{' '}
            {typeof currentOperations.cancellation?.p95_seconds === 'number'
              ? `${currentOperations.cancellation.p95_seconds.toFixed(3)}s (${currentOperations.cancellation.sample_count})`
              : 'no samples'}
          </p>
        </>
      )}
      {currentOperations?.diagnostics?.alerts.map((alert) => (
        <p className="error" role="status" key={alert.code}>
          Operator check: {alert.message}
        </p>
      ))}
      {!!currentOperations?.diagnostics?.alerts.length && (
        <p className="help-text" aria-label="Operational alert delivery status">
          Alert delivery: in-app polling only · no external notifications sent.
        </p>
      )}
      {error && (
        <div className="error" role="alert">
          <span>{error}</span>{' '}
          <button type="button" className="secondary-button" onClick={() => refreshRef.current()}>
            Retry refresh
          </button>
        </div>
      )}
      {currentRuns.length === 0 && !error && <p className="help-text">No saved runs yet.</p>}
      <div className="recent-list">
        {currentRuns.map((run) => (
          <button key={run.run_id} onClick={() => onSelect(run)} type="button">
            <code>{run.run_id.slice(0, 13)}…</code>
            <span>{run.variant} / {run.schedule}</span>
            <small>{run.status}{run.verdict ? ` · ${run.verdict}` : ''}</small>
          </button>
        ))}
      </div>
    </section>
  )
}
