import { useEffect, useState, type Dispatch, type SetStateAction } from 'react'

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
  onUpdate: Dispatch<SetStateAction<DurableRun | null>>
  operatorToken: string
  canOperate: boolean
  accountMode: boolean
}

export default function DurableMonitor({
  initial, onUpdate, operatorToken, canOperate, accountMode,
}: Props) {
  const [events, setEvents] = useState<Event[]>([])
  const [error, setError] = useState('')
  const [exportDigest, setExportDigest] = useState<string | null>(null)
  const runId = initial.run_id

  useEffect(() => {
    setExportDigest(null)
  }, [runId, initial.version])

  useEffect(() => {
    let mounted = true
    setEvents([])

    async function refresh() {
      try {
        const snapshotResponse = await fetch(`/api/runs/${runId}`)
        if (!snapshotResponse.ok) throw new Error('Snapshot refresh failed')
        const run = (await snapshotResponse.json()) as DurableRun
        let cursor = 0
        let more = true
        const history: Event[] = []
        for (let pageNumber = 0; pageNumber < 10 && more; pageNumber++) {
          const response = await fetch(`/api/runs/${runId}/events?after=${cursor}`)
          if (!response.ok) throw new Error('History refresh failed')
          const page = (await response.json()) as {
            events: Event[]
            next_after: number
            has_more: boolean
          }
          if (page.has_more && page.next_after <= cursor) {
            throw new Error('Transition cursor did not advance')
          }
          history.push(...page.events)
          cursor = page.next_after
          more = page.has_more
        }
        if (mounted) {
          // Overlapping SSE and polling responses can arrive out of order.
          // Never rewind a run or replace the active selection with an old one.
          onUpdate((current) => (
            current?.run_id === runId && run.version > current.version
              ? run : current
          ))
          // Also keep the longest authoritative event prefix.
          setEvents((current) => {
            const prior = current.at(-1)?.version ?? 0
            const next = history.at(-1)?.version ?? 0
            return next >= prior ? history : current
          })
          setError(more ? 'Timeline is partial; refresh to inspect more events.' : '')
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
        method: 'POST',
        headers: accountMode ? {} : { 'X-Operator-Token': operatorToken },
      })
      if (!response.ok) throw new Error('Cancellation request failed')
      const updated = (await response.json()) as DurableRun
      onUpdate((current) => (
        current?.run_id === runId && updated.version > current.version
          ? updated : current
      ))
    } catch {
      setError('Cancellation request could not be confirmed.')
    }
  }

  async function exportBundle() {
    if (!initial.witness || initial.status !== 'COMPLETE' || !canOperate) return
    setError('')
    setExportDigest(null)
    try {
      const recorded = await fetch(`/api/runs/${runId}/exports`, {
        method: 'POST',
        headers: accountMode ? {} : { 'X-Operator-Token': operatorToken },
      })
      if (!recorded.ok) throw new Error('Export record was rejected')
      const audit = (await recorded.json()) as {
        run_version: number
        artifact_sha256: string
        artifact_bytes: number
      }
      if (audit.run_version !== initial.version) {
        throw new Error('The run changed during export')
      }
      const response = await fetch(`/api/runs/${runId}/bundle`)
      if (!response.ok) throw new Error('Bundle export rejected')
      const blob = await response.blob()
      const bytes = await blob.arrayBuffer()
      const digest = Array.from(
        new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)),
        (byte) => byte.toString(16).padStart(2, '0'),
      ).join('')
      if (
        digest !== audit.artifact_sha256 ||
        blob.size !== audit.artifact_bytes ||
        response.headers.get('X-Clashbolt-Artifact-Sha256') !== digest
      ) {
        throw new Error('Downloaded artifact differs from immutable export record')
      }
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `clashbolt-witness-${runId}.zip`
      link.click()
      URL.revokeObjectURL(url)
      setExportDigest(digest)
    } catch {
      setError('Portable evidence export could not be verified or completed.')
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
    <section id="run-timeline" className="panel operations" aria-labelledby="operations-title">
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
          onClick={() => void cancel()}
          disabled={!canOperate || finished || initial.cancel_requested}
        >
          Request cancellation
        </button>
        <button
          type="button" className="secondary-button"
          onClick={() => void exportEvidence()} disabled={!initial.witness}
        >
          Export synthetic evidence (JSON)
        </button>
        <button
          type="button" className="secondary-button"
          onClick={() => void exportBundle()}
          disabled={!canOperate || !initial.witness || initial.status !== 'COMPLETE'}
        >
          Export verifier bundle (ZIP)
        </button>
      </div>
      {exportDigest && (
        <p className="help-text" aria-label="Audited evidence export">
          Verified synthetic ZIP SHA256: {exportDigest}
        </p>
      )}
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
