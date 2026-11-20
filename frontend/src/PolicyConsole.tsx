import { useState } from 'react'

type Variant = 'racy' | 'locked'
type Schedule = 'overlap' | 'serial'
type Rule = { id: string; rule_hash: string; state: string }
type Capability = { capability_id: string; token: string; max_calls: number }
type Plan = { id: string; plan: { variant: Variant; schedule: Schedule } }
type Props = {
  variant: Variant
  schedule: Schedule
  onApproved: (id: string) => void
  operator: string
}

async function request<T>(url: string, body?: unknown, token?: string, bearer?: string): Promise<T> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  if (token) headers['X-Operator-Token'] = token
  if (bearer) headers.Authorization = `Bearer ${bearer}`
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

export default function PolicyConsole({ variant, schedule, onApproved, operator }: Props) {
  const [rule, setRule] = useState<Rule | null>(null)
  const [capability, setCapability] = useState<Capability | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [output, setOutput] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

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
      })
      setRule(result)
      setCapability(null)
      setPlan(null)
      return { status: result.state, proposal_id: result.id, rule_hash: result.rule_hash }
    })
  }

  async function approve() {
    if (!rule) return
    await action(async () => {
      const result = await request<Rule>(
        `/api/oracle/proposals/${rule.id}/approve`,
        { expected_hash: rule.rule_hash, reviewer: 'local-operator' },
        operator,
      )
      setRule({ ...rule, state: result.state })
      onApproved(rule.id)
      return result
    })
  }

  async function createPlan() {
    if (!rule) return
    await action(async () => {
      const saved = await request<Plan>(
        '/api/agent/plans',
        { proposal_id: rule.id, strategy: 'semantic', variant, seed: 0 },
      )
      setPlan(saved)
      return saved
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
    if (!capability) return
    await action(() => request(
      '/api/agent/tools/run', {
        variant: plan!.plan.variant, schedule: plan!.plan.schedule, plan_id: plan!.id,
      }, undefined, capability.token,
    ))
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
        Use the operator token entered above. It stays in memory, not browser storage.
      </p>
      <div className="policy-actions">
        <button type="button" onClick={() => void propose()} disabled={busy}>1. Propose fixed rule</button>
        <button type="button" onClick={() => void approve()} disabled={busy || !rule || rule.state !== 'PROPOSED' || !operator}>2. Approve exact hash</button>
        <button type="button" onClick={() => void createPlan()} disabled={busy || rule?.state !== 'APPROVED'}>3. Propose typed plan</button>
        <button type="button" onClick={() => void modelPlan()}
          disabled={busy || rule?.state !== 'APPROVED' || !operator}>
          3b. Suggest local model plan
        </button>
        <button type="button" onClick={() => void issue()} disabled={busy || rule?.state !== 'APPROVED' || !plan || !operator}>4. Issue one-call capability</button>
        <button type="button" onClick={() => void useTool()} disabled={busy || !capability || !plan}>5. Execute approved synthetic tool</button>
        <button type="button" onClick={() => void revoke()} disabled={busy || !capability || !operator}>Revoke tool capability</button>
        <button type="button" onClick={() => void action(() => request('/api/tool-audit', undefined, operator))} disabled={busy || !operator}>Review tool audit</button>
        <button type="button" onClick={() => void action(() => request('/api/agent/evaluate', { trials: 8 }, operator))} disabled={busy || !operator}>Compare deterministic baselines</button>
      </div>
      <p className="help-text">
        Local model mode is optional and unpromoted. Invalid or unavailable model
        responses fall back to the deterministic planner; no model can issue authority.
      </p>
      <p className="help-text">
        Selected form: {variant} / {schedule}. Approved plan:
        {plan ? ` ${plan.plan.variant} / ${plan.plan.schedule}` : ' none'}.
        The broker only executes the exact saved plan bound to its capability.
      </p>
      {rule && <div className="policy-reference">
        <span>RULE STATUS: {rule.state}</span><code title={rule.rule_hash}>{rule.rule_hash.slice(0, 24)}…</code>
      </div>}
      {error && <p className="error" role="alert">{error}</p>}
      {output && <pre className="policy-output" aria-label="Last operation result">{output}</pre>}
    </section>
  )
}
