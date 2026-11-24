import { useEffect, useState } from 'react'
import type { DurableRun } from './DurableMonitor'

type Decision = 'SUPPORTED' | 'WITHHELD'
type Review = {
  review_id: string
  decision: Decision
  reason: string
  current: boolean
  created_at: string
}
type Snapshot = {
  run_id: string
  project_id: string
  run_version: number
  evidence_sha256: string | null
  replay_verified: boolean
  scope: 'SYNTHETIC_ONLY'
  independent_external_reproduction: false
  reviews: Review[]
}
type Props = {
  run: DurableRun
  projectId: string
  role: 'viewer' | 'operator' | 'reviewer'
  replayStatus: string
}

export default function WitnessReviews({ run, projectId, role, replayStatus }: Props) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [error, setError] = useState('')

  const runId = run.run_id
  const runVersion = run.version
  const evidenceHash = run.witness?.evidence_sha256 ?? null
  const currentSnapshot = snapshot?.run_id === runId
    && snapshot.project_id === projectId
    && snapshot.run_version === runVersion
    && snapshot.evidence_sha256 === evidenceHash ? snapshot : null

  useEffect(() => {
    const controller = new AbortController()
    setSnapshot(null)
    setError('')
    fetch(`/api/runs/${runId}/reviews`, {
      cache: 'no-store', signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error('Review evidence is unavailable')
        const data = (await response.json()) as Snapshot
        if (data.run_id !== runId || data.project_id !== projectId
          || data.run_version !== runVersion || data.evidence_sha256 !== evidenceHash
          || data.scope !== 'SYNTHETIC_ONLY' || data.independent_external_reproduction !== false
          || !Array.isArray(data.reviews) || typeof data.replay_verified !== 'boolean'
        ) throw new Error('Review data did not match the current witness')
        if (!controller.signal.aborted) setSnapshot(data)
      })
      .catch(() => {
        if (!controller.signal.aborted) setError('Review data is unavailable for this project.')
      })
    return () => controller.abort()
  }, [runId, runVersion, evidenceHash, projectId, replayStatus, refresh])

  async function decide(decision: Decision) {
    if (!currentSnapshot || reason.trim().length < 12) return
    setBusy(true)
    setError('')
    try {
      const response = await fetch(`/api/runs/${run.run_id}/reviews`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_run_version: currentSnapshot.run_version,
          expected_evidence_sha256: currentSnapshot.evidence_sha256,
          decision,
          reason: reason.trim(),
        }),
      })
      if (!response.ok) {
        const payload = (await response.json().catch(() => ({}))) as { detail?: string }
        throw new Error(payload.detail ?? 'Review was rejected by the server')
      }
      setReason('')
      setRefresh((current) => current + 1)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Review failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel" aria-label="Independent synthetic witness review">
      <div className="panel-kicker">WITNESS REVIEW AUDIT</div>
      <h2>Independent evidence review</h2>
      <p className="help-text">
        Named reviewers can record a reason bound to an exact, worker-verified
        synthetic replay. This is not external application reproduction.
      </p>
      <p className="help-text" aria-label="Synthetic replay readiness">
        Worker replay: {currentSnapshot?.replay_verified ? 'VERIFIED SYNTHETIC REPLAY'
          : 'NOT VERIFIED'} · External reproduction: NOT ESTABLISHED.
      </p>
      <button className="secondary-button" type="button"
        onClick={() => setRefresh((current) => current + 1)}>
        Refresh evidence review
      </button>
      {currentSnapshot?.reviews.map((entry) => (
        <p className="help-text" key={entry.review_id}>
          {entry.decision} · {entry.current ? 'CURRENT' : 'SUPERSEDED'} ·
          {' '}{entry.reason}
        </p>
      ))}
      {role === 'reviewer' && currentSnapshot && (
        <>
          <label className="field-label" htmlFor="review-reason">
            Your review reason (at least 12 characters)
          </label>
          <textarea
            id="review-reason" rows={3} maxLength={400}
            value={reason} onChange={(event) => setReason(event.target.value)}
          />
          <div className="policy-actions">
            <button type="button" disabled={busy || reason.trim().length < 12
              || run.status !== 'COMPLETE' || run.verdict !== 'FAIL'}
              onClick={() => void decide('WITHHELD')}>
              Withhold synthetic review
            </button>
            <button type="button" disabled={busy || reason.trim().length < 12
              || !currentSnapshot.replay_verified || run.status !== 'COMPLETE'
              || run.verdict !== 'FAIL'}
              onClick={() => void decide('SUPPORTED')}>
              Support verified synthetic replay
            </button>
          </div>
        </>
      )}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  )
}
