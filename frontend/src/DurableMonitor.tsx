import { useEffect, useState } from 'react'

export type DurableRun = {
  run_id: string
  variant: 'racy' | 'locked'
  schedule: 'overlap' | 'serial'
  status: 'PENDING' | 'RUNNING' | 'CANCELLING' | 'CANCELLED' | 'COMPLETE' | 'UNKNOWN'
  version: number
  verdict: 'PASS' | 'FAIL' | 'UNKNOWN' | null
  cancel_requested: boolean
  execution_backend: 'memory' | 'native-postgresql'
  step_budget: number
  consumed_steps: number
  witness: {
    variant: 'racy' | 'locked'
    schedule: 'overlap' | 'serial'
    complete: boolean
    verdict: 'PASS' | 'FAIL' | 'UNKNOWN'
    fixture_hash: string
    evidence_sha256: string
    observed_steps: {
      sequence: number
      client: string
      checkpoint: string
      observed_stock?: number
      accepted?: boolean
      stock_after?: number
      confirmed_after?: number
    }[]
    final_state: { initial_stock: number; remaining_stock: number; confirmed_count: number }
    oracle: { id: string; version: string; sha256: string; authority: string }
    target_id: string
    target_revision: string
    fixture: { initial_stock: number; clients: string[]; quantity_each: number }
    requested_gates: string[]
    scope: string
  } | null
}

type Event = {
  version: number
  status: string
  note: string
  created_at: string
}

type Props = {
  initial: DurableRun
  onUpdate: (run: DurableRun) => void
  operatorToken: string
}

export default function DurableMonitor({ initial, onUpdate, operatorToken }: Props) {
  const [events, setEvents] = useState<Event[]>([])
  const [error, setError] = useState('')
  const runId = initial.run_id

  useEffect(() => {
    let mounted = true

    async function refresh() {
      try {
        const [snapshotResponse, eventsResponse] = await Promise.all([
          fetch(`/api/runs/${runId}`),
          fetch(`/api/runs/${runId}/events?after=0`),
        ])
        if (!snapshotResponse.ok || !eventsResponse.ok) throw new Error('Refresh failed')
        const run = (await snapshotResponse.json()) as DurableRun
        const history = (await eventsResponse.json()) as { events: Event[] }
        if (mounted) {
          onUpdate(run)
          setEvents(history.events)
          setError('')
        }
      } catch {
        if (mounted) setError('Cannot refresh the authoritative run snapshot.')
      }
    }

    void refresh()
    const timer = setInterval(() => void refresh(), 3000)
    const stream = new EventSource(`/api/runs/${runId}/stream?after=${initial.version}`)
    stream.addEventListener('transition', () => void refresh())
    stream.onerror = () => void refresh()
    return () => {
      mounted = false
      clearInterval(timer)
      stream.close()
    }
  }, [runId, onUpdate])

  async function cancel() {
    try {
      const response = await fetch(`/api/runs/${runId}/cancel`, {
        method: 'POST', headers: { 'X-Operator-Token': operatorToken },
      })
      if (!response.ok) throw new Error('Cancellation request failed')
      onUpdate((await response.json()) as DurableRun)
    } catch {
      setError('Cancellation request could not be confirmed.')
    }
  }

  async function exportEvidence() {
    if (!initial.witness) return
    const bytes = JSON.stringify(initial.witness, null, 2)
    const url = URL.createObjectURL(new Blob([bytes], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `clashbolt-synthetic-${runId}.json`
    link.click()
    URL.revokeObjectURL(url)
  }

  const finished = ['COMPLETE', 'UNKNOWN', 'CANCELLED'].includes(initial.status)

  return (
    <section className="panel operations" aria-labelledby="operations-title">
      <div className="panel-kicker">03 / DURABLE RUN OPERATIONS</div>
      <h2 id="operations-title">PostgreSQL run timeline</h2>
      <p className="panel-description">
        The database owns this state. SSE is an advisory update; this view regularly
        reloads the authoritative snapshot after connection loss.
      </p>
      <div className="run-overview">
        <span className={`run-status status-${initial.status.toLowerCase()}`}>{initial.status}</span>
        <code title={runId}>Run {runId.slice(0, 13)}…</code>
        <span>Version {initial.version}</span>
        <span>Controlled gates: {initial.consumed_steps}/{initial.step_budget}</span>
        <span>{initial.execution_backend === 'native-postgresql'
          ? 'Disposable PostgreSQL target' : 'In-memory target'}</span>
      </div>
      {initial.status === 'PENDING' && (
        <p className="help-text">Queued. Start the separate Python worker to process this run.</p>
      )}
      {initial.status === 'CANCELLING' && (
        <p className="help-text">An in-flight effect cannot be undone. Awaiting reconciliation.</p>
      )}
      <div className="operation-buttons">
        <button
          type="button" className="secondary-button"
          onClick={() => void cancel()} disabled={finished || initial.cancel_requested}
        >
          Request cancellation
        </button>
        <button
          type="button" className="secondary-button"
          onClick={() => void exportEvidence()} disabled={!initial.witness}
        >
          Export synthetic evidence (JSON)
        </button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <ol className="transition-list">
        {events.map((event) => (
          <li key={event.version}>
            <code>v{event.version}</code>
            <strong>{event.status}</strong>
            <span>{event.note}</span>
          </li>
        ))}
      </ol>
    </section>
  )
}
