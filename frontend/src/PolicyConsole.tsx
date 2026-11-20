import { useEffect, useState } from 'react'

type Variant = 'racy' | 'locked'
type Schedule = 'overlap' | 'serial'
type Rule = {
  id: string
  rule_hash: string
  state: string
  evidence?: {
    sha256: string
    verified: boolean
    test_vectors: { id: string; actual: string; expected: string }[]
  }
}
type Feedback = {
  decision: {
    action: string
    reason?: string
    next_step_cost: number
    automatically_authorized: boolean
  }
  next_plan_id: string | null
}
type Capability = { capability_id: string; token: string; max_calls: number }
type Plan = { id: string; plan: { variant: Variant; schedule: Schedule } }
type Props = {
  variant: Variant
  schedule: Schedule
  onApproved: (id: string) => void
  operator: string
  accountRole?: 'viewer' | 'operator' | 'reviewer'
}

async function request<T>(
  url: string, body?: unknown, token?: string, bearer?: string, reviewer?: string,
): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['X-Operator-Token'] = token
  if (bearer) headers.Authorization = `Bearer ${bearer}`
  if (reviewer) headers['X-Reviewer-Token'] = reviewer
  const response = await fetch(url, {
    method: body === undefined ? 'GET' : 'POST',
    headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (!response.ok) {
    const reason = (await response.json().catch(() => ({}))) as { detail?: string }
    throw new Error(reason.detail ?? `Request rejected (${response.status})`)
  }
  return (await response.json()) as T
}

export default function PolicyConsole({
  variant, schedule, onApproved, operator, accountRole,
}: Props) {
  const [rule, setRule] = useState<Rule | null>(null)
  const [proposals, setProposals] = useState<Rule[]>([])
  const [capability, setCapability] = useState<Capability | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [rationale, setRationale] = useState(
    'Inventory confirmations cannot exceed available initial stock.'
  )
  const [toolFinished, setToolFinished] = useState(false)
  const [output, setOutput] = useState('')
  const [error, setError] = useState('')
  const [reviewerToken, setReviewerToken] = useState('')
  const [busy, setBusy] = useState(false)
  const canOperate = accountRole ? accountRole === 'operator' : !!operator
  const canReview = accountRole ? accountRole === 'reviewer' : !!operator

  useEffect(() => {
    let mounted = true
    request<{ proposals: Rule[] }>('/api/oracle/proposals')
      .then((response) => {
        if (!mounted) return
        setProposals(response.proposals)
        setRule((current) => current ?? (
          response.proposals.find((item) => item.state === 'PROPOSED') ?? null
        ))
      })
      .catch(() => {
        if (mounted) setProposals([])
      })
    return () => { mounted = false }
  }, [accountRole])

  async function action(callback: () => Promise<unknown>) {
    setBusy(true)
    setError('')
    try {
      const result = await callback()
      setOutput(JSON.stringify(result, null, 2))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Operation failed')
    } finally {
      setBusy(false)
    }
  }

  async function propose() {
    await action(async () => {
      const result = await request<Rule>('/api/oracle/proposals', {
        proposed_by: 'deterministic-workshop',
        candidate: { rationale },
      }, operator)
      setRule(result)
      setProposals((current) => [result, ...current])
      setCapability(null)
      setPlan(null)
      setToolFinished(false)
      return result
    })
  }

  async function approve() {
    if (!rule) return
    await action(async () => {
      const result = await request<Rule>(
        `/api/oracle/proposals/${rule.id}/approve`,
        { expected_hash: rule.rule_hash, reviewer: 'local-operator' },
        operator, undefined, reviewerToken,
      )
      setRule({ ...rule, state: result.state })
      setProposals((current) => current.map((item) =>
        item.id === rule.id ? { ...item, state: result.state } : item
      ))
      onApproved(rule.id)
      return result
    })
  }

  async function createPlan() {
    if (!rule) return
    await action(async () => {
      const saved = await request<Plan>(
        '/api/agent/plans',
        { proposal_id: rule.id, strategy: 'semantic', variant, seed: 0 }, operator,
      )
      setPlan(saved)
      setCapability(null)
      setToolFinished(false)
      return saved
    })
  }

  async function frontierPlan() {
    if (!rule) return
    await action(async () => {
      const frontier = await request<{
        policy_version: string
        promotion_eligible: boolean
        candidates: {
          schedule: Schedule
          priority: number
          attempts: number
          confirmed_failures: number
        }[]
      }>(`/api/agent/frontier/${rule.id}?variant=${variant}`)
      const saved = await request<Plan>(
        `/api/agent/frontier/${rule.id}/plans`,
        { proposal_id: rule.id, variant }, operator,
      )
      setPlan(saved)
      setCapability(null)
      setToolFinished(false)
      return { frontier, next_plan: saved }
    })
  }

  async function modelPlan() {
    if (!rule) return
    await action(async () => {
      const suggested = await request<Plan>(
        '/api/agent/plans/model',
        { proposal_id: rule.id, variant }, operator,
      )
      setPlan(suggested)
      setCapability(null)
      setToolFinished(false)
      return suggested
    })
  }

  async function issue() {
    if (!rule) return
    await action(async () => {
      const result = await request<Capability>(
        '/api/tool-capabilities',
        {
          proposal_id: rule.id, variant: plan!.plan.variant,
          schedule: plan!.plan.schedule, max_calls: 1, plan_id: plan!.id,
        },
        operator,
      )
      setCapability(result)
      return { capability_id: result.capability_id, max_calls: result.max_calls,
        message: 'Capability is held in this page only; it is not displayed or saved.' }
    })
  }

  async function useTool() {
    if (!capability || !plan) return
    await action(async () => {
      const result = await request(
        '/api/agent/tools/run', {
          variant: plan.plan.variant, schedule: plan.plan.schedule, plan_id: plan.id,
        }, undefined, capability.token,
      )
      setToolFinished(true)
      setCapability(null)
      return result
    })
  }

  async function reviewFeedback() {
    if (!plan) return
    await action(async () => {
      const result = await request<Feedback>(
        `/api/agent/plans/${plan.id}/feedback`, { remaining_budget: 4 }, operator,
      )
      const gaps = await request<{ gaps: { classification: string; reason: string }[] }>(
        `/api/agent/plans/${plan.id}/gaps`,
      )
      if (result.next_plan_id) {
        const next = await request<Plan>(
          `/api/agent/plans/${result.next_plan_id}`,
        )
        setPlan(next)
        setToolFinished(false)
        setCapability(null)
      }
      return { ...result, evidence_gaps: gaps.gaps }
    })
  }

  async function revoke() {
    if (!capability) return
    await action(async () => {
      const result = await request(
        `/api/tool-capabilities/${capability.capability_id}/revoke`, {},
        operator,
      )
      setCapability(null)
      return result
    })
  }

  return (
    <section className="panel policy-console" aria-labelledby="policy-title">
      <div className="panel-kicker">04 / RULE REVIEW + CAPABILITY BROKER</div>
      <h2 id="policy-title">Deterministic agent safety lab</h2>
      <p className="panel-description">
        A proposed rule cannot authorize execution. The local operator must approve its
        exact hash and issue a variant/schedule-bound token. This is not a deployed AI agent.
      </p>
      <p className="help-text">
        The operator token above controls execution. A separate reviewer secret can
        be required for approving immutable rules.
      </p>
      <label className="field-label" htmlFor="reviewer-token">
        Independent reviewer token
      </label>
      <input
        id="reviewer-token" type="password" autoComplete="off"
        value={reviewerToken}
        onChange={(event) => setReviewerToken(event.target.value)}
        placeholder="CLASHBOLT_REVIEWER_TOKEN (when configured)"
      />
      <label className="field-label" htmlFor="rule-rationale">
        Candidate rule rationale
      </label>
      <input
        id="rule-rationale" type="text" maxLength={240}
        value={rationale} onChange={(event) => setRationale(event.target.value)}
      />
      <p className="help-text">
        Rationale is descriptive only. The fixed rule and checker cannot be changed here.
      </p>
      <label className="field-label" htmlFor="rule-selection">
        Existing rule proposals
      </label>
      <select
        id="rule-selection" value={rule?.id ?? ''}
        onChange={(event) => {
          setRule(proposals.find((item) => item.id === event.target.value) ?? null)
          setPlan(null)
          setCapability(null)
          setToolFinished(false)
        }}
      >
        <option value="">Select an existing proposal</option>
        {proposals.map((item) => (
          <option key={item.id} value={item.id}>
            {item.state} · {item.id.slice(0, 12)} · {item.rule_hash.slice(0, 8)}
          </option>
        ))}
      </select>
      <div className="policy-actions">
        <button type="button" onClick={() => void propose()} disabled={busy || rationale.length < 12 || !canOperate}>
          1. Validate candidate rule
        </button>
        <button type="button" onClick={() => void approve()} disabled={busy || !rule || rule.state !== 'PROPOSED' || !canReview}>2. Approve exact hash</button>
        <button type="button" onClick={() => void createPlan()} disabled={busy || rule?.state !== 'APPROVED' || !canOperate}>3. Propose typed plan</button>
        <button type="button" onClick={() => void frontierPlan()}
          disabled={busy || rule?.state !== 'APPROVED' || !canOperate}>
          3a. Rank semantic conflict frontier
        </button>
        <button type="button" onClick={() => void modelPlan()}
          disabled={busy || rule?.state !== 'APPROVED' || !operator}>
          3b. Suggest local model plan
        </button>
        <button type="button" onClick={() => void issue()} disabled={busy || rule?.state !== 'APPROVED' || !plan || !canOperate}>4. Issue one-call capability</button>
        <button type="button" onClick={() => void useTool()} disabled={busy || !capability || !plan || toolFinished || !canOperate}>
          5. Execute approved synthetic tool
        </button>
        <button type="button" onClick={() => void reviewFeedback()}
          disabled={busy || !plan || !toolFinished || !canOperate}>
          6. Persist evidence feedback / bounded replan
        </button>
        <button type="button" onClick={() => void revoke()} disabled={busy || !capability || !canOperate}>Revoke tool capability</button>
        <button type="button" onClick={() => void action(() => request('/api/tool-audit', undefined, operator))} disabled={busy || !canOperate}>Review tool audit</button>
        <button type="button" onClick={() => void action(() => request('/api/agent/evaluate', { trials: 8 }, operator))} disabled={busy || !operator}>Compare deterministic baselines</button>
      </div>
      <p className="help-text">
        Local model mode is optional and unpromoted. Invalid or unavailable model
        responses fall back to the deterministic planner; no model can issue authority.
        The frontier ranks only the two shipped schedules using prior verified receipts;
        its scores are not held-out research evidence.
      </p>
      <p className="help-text">
        Selected form: {variant} / {schedule}. Approved plan:
        {plan ? ` ${plan.plan.variant} / ${plan.plan.schedule}` : ' none'}.
        The broker only executes the exact saved plan bound to its capability.
      </p>
      {rule && <div className="policy-reference">
        <span>RULE STATUS: {rule.state}</span>
        <code title={rule.rule_hash}>{rule.rule_hash.slice(0, 24)}…</code>
        {rule.evidence && <>
          <span>VECTOR CHECKS: {rule.evidence.verified ? 'VERIFIED' : 'UNVERIFIED'}</span>
          <code title={rule.evidence.sha256}>{rule.evidence.sha256.slice(0, 24)}…</code>
          <span>{rule.evidence.test_vectors.map((caseItem) => (
            `${caseItem.id}: ${caseItem.actual}`
          )).join(' · ')}</span>
        </>}
      </div>}
      {error && <p className="error" role="alert">{error}</p>}
      {output && <pre className="policy-output" aria-label="Last operation result">{output}</pre>}
    </section>
  )
}
