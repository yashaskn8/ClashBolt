import { useEffect, useState } from 'react'
import type { DurableRun } from './DurableMonitor'

type Props = {
  onSelect: (run: DurableRun) => void
  operatorReady: boolean
  accountMode: boolean
  operatorToken: string
}
type Operations = {
  responsive_workers: number
  runs: Record<string, number>
  verifications: Record<string, number>
  diagnostics?: {
    status: 'NORMAL' | 'ATTENTION'
    alerts: { code: string; message: string }[]
  }
}

export default function RecentRuns({
  onSelect, operatorReady, accountMode, operatorToken,
}: Props) {
  const [runs, setRuns] = useState<DurableRun[]>([])
  const [operations, setOperations] = useState<Operations | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let mounted = true
    let refreshing = false
    let refreshAgain = false
    const controller = new AbortController()
    // Role, project, and credential changes invalidate the previous view.
    setRuns([])
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
          setRuns(parsed.runs)
          setError('')
        }
      } catch {
        if (mounted) {
          // Never continue showing an index after authorization or DB loss.
          setRuns([])
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
        if (mounted) setOperations(status)
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
    const timer = setInterval(() => void refresh(), 5000)
    return () => {
      mounted = false
      clearInterval(timer)
      controller.abort()
    }
  }, [operatorReady, accountMode, operatorToken])

  return (
    <section className="panel recent-runs" aria-labelledby="recent-title">
      <div className="panel-kicker">03 / RECOVERY INDEX</div>
      <h2 id="recent-title">Reopen a durable run</h2>
      <p className="panel-description">
        Select a PostgreSQL run after reloading the page or restarting the coordinator.
        This is a local-only workspace.
      </p>
      {operatorReady && operations && (
        <p className="help-text">
          Live workers: {operations.responsive_workers} ·
          Pending runs: {operations.runs.PENDING ?? 0} ·
          Verification queue: {operations.verifications.PENDING ?? 0}
        </p>
      )}
      {operatorReady && operations?.diagnostics?.alerts.map((alert) => (
        <p className="error" role="status" key={alert.code}>
          Operator check: {alert.message}
        </p>
      ))}
      {error && <p className="error" role="alert">{error}</p>}
      {runs.length === 0 && !error && <p className="help-text">No saved runs yet.</p>}
      <div className="recent-list">
        {runs.map((run) => (
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
