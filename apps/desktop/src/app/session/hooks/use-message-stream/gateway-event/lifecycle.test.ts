import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { dispatchNativeNotification } from '@/store/native-notifications'

import { handleLifecycleEvent } from './lifecycle'
import type { GatewayEventContext } from './types'

vi.mock('@/store/native-notifications', () => ({
  dispatchNativeNotification: vi.fn()
}))

// `session.message.created` — cron delivering `deliver=origin` output back
// into the Desktop/TUI/CLI stored session that created the job (see
// cron/session_delivery.py). The backend addresses this event by STORED
// session id, a different keyspace from the runtime `sid` used everywhere
// else in this dispatcher.
function sessionMessageEvent({
  activeSessionId,
  jobName,
  preview,
  runtimeIdByStoredSessionId = new Map(),
  storedSessionId
}: {
  activeSessionId: null | string
  jobName?: string
  preview?: string
  runtimeIdByStoredSessionId?: Map<string, string>
  storedSessionId: string
}): GatewayEventContext {
  return {
    deps: {
      activeGatewayProfile: 'default',
      activeSessionIdRef: { current: activeSessionId },
      hydrateFromStoredSession: vi.fn(),
      lastCwdInfoSessionRef: { current: null },
      queryClient: { invalidateQueries: vi.fn() },
      refreshHermesConfig: vi.fn(),
      runtimeIdByStoredSessionIdRef: { current: runtimeIdByStoredSessionId },
      scheduleSessionsRefresh: vi.fn(),
      sessionInterrupted: () => false,
      sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn(state => state),
      upsertToolCall: vi.fn()
    },
    event: { profile: 'default', session_id: '', type: 'session.message.created' },
    explicitSid: '',
    fromActiveSource: () => true,
    isActiveEvent: false,
    occurredAt: Date.now() / 1000,
    payload: { job_name: jobName, preview, session_id: storedSessionId },
    scheduleConfigRefresh: vi.fn(),
    sessionId: activeSessionId
  } as unknown as GatewayEventContext
}

describe('handleLifecycleEvent session.message.created (cron session delivery)', () => {
  beforeEach(() => {
    vi.mocked(dispatchNativeNotification).mockClear()
  })

  afterEach(() => {
    vi.mocked(dispatchNativeNotification).mockClear()
  })

  it('claims the event', () => {
    const handled = handleLifecycleEvent(
      sessionMessageEvent({ activeSessionId: null, storedSessionId: 'stored-1' })
    )

    expect(handled).toBe(true)
  })

  it('fires a native notification addressed by the stored session id when this window never opened it', () => {
    handleLifecycleEvent(
      sessionMessageEvent({
        activeSessionId: 'runtime-other',
        jobName: 'Morning reminder',
        preview: 'Your reminder fired.',
        storedSessionId: 'stored-unopened'
      })
    )

    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({
        body: 'Your reminder fired.',
        kind: 'sessionMessage',
        sessionId: 'stored-unopened',
        title: 'Morning reminder'
      })
    )
  })

  it('resolves the live runtime id when the stored session is open in this window', () => {
    const map = new Map([['stored-open', 'runtime-open']])

    handleLifecycleEvent(
      sessionMessageEvent({
        activeSessionId: 'runtime-other',
        runtimeIdByStoredSessionId: map,
        storedSessionId: 'stored-open'
      })
    )

    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: 'runtime-open' })
    )
  })

  it('still resolves to the runtime id even when that session is the one on screen (shouldFire suppresses it downstream)', () => {
    const map = new Map([['stored-open', 'runtime-open']])

    handleLifecycleEvent(
      sessionMessageEvent({
        activeSessionId: 'runtime-open',
        runtimeIdByStoredSessionId: map,
        storedSessionId: 'stored-open'
      })
    )

    // Passing the resolved runtime id (not the stored id) is what lets
    // dispatchNativeNotification's shouldFire correctly compare against
    // $activeSessionId and suppress the on-screen case.
    expect(dispatchNativeNotification).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: 'runtime-open' })
    )
  })

  it('does not dispatch a notification when the payload carries no session id', () => {
    handleLifecycleEvent(sessionMessageEvent({ activeSessionId: null, storedSessionId: '' }))

    expect(dispatchNativeNotification).not.toHaveBeenCalled()
  })
})
