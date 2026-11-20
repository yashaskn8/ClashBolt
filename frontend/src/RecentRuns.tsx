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
}

export default function RecentRuns({
  onSelect, operatorReady, accountMode, operatorToken,
}: Props) {
  const [runs, setRuns] = useState<DurableRun[]>([])
  const [operations, setOperations] = useState<Operations | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let mounted = true
    async function refresh() {
      try {
        const response = await fetch('/api/runs?limit=10')
        if (!response.ok) throw new Error('Could not refresh run index')
        const parsed = (await response.json()) as { runs: DurableRun[] }
        if (mounted) { setRuns(parsed.runs); setError('') }
        if (operatorReady) {
          const status = await fetch('/api/operations', {
            headers: accountMode ? {} : { 'X-Operator-Token': operatorToken },
          })
          if (mounted && status.ok) {
            setOperations((await status.json()) as Operations)
          }
        } else if (mounted) {
          setOperations(null)
        }
      } catch {
        if (mounted) setError('Recent runs unavailable; check PostgreSQL and the API.')
      }
    }
    void refresh()
    const timer = setInterval(() => void refresh(), 5000)
    return () => { mounted = false; clearInterval(timer) }
  }, [operatorReady, accountMode, operatorToken])

  return (
    <section className="panel recent-runs" aria-labelledby="recent-title">
      <div className="panel-kicker">03 / RECOVERY INDEX</div>
      <h2 id="recent-title">Reopen a durable run</h2>
      <p className="panel-description">
        Select a PostgreSQL run after reloading the page or restarting the coordinator.
        This is a local-only workspace.
      </p>
      {operations && (
        <p className="help-text">
          Live workers: {operations.responsive_workers} ·
          Pending runs: {operations.runs.PENDING ?? 0} ·
          Verification queue: {operations.verifications.PENDING ?? 0}
        </p>
      )}
      {error && <p className="error" role="alert">{error}</p>
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
