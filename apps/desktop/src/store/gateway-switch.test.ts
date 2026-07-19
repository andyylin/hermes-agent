import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $sessionsLimit, resetSessionsLimit, SIDEBAR_SESSIONS_PAGE_SIZE } from '@/store/layout'
import {
  $activeProjectId,
  $projectScope,
  $projects,
  $projectsRpcAvailable,
  $projectSessionAssignmentsAvailable,
  $projectTree,
  $projectTreeLoading,
  $removedSessionIds,
  $reposScanning,
  $sessionProjectAssignments,
  ALL_PROJECTS
} from '@/store/projects'
import {
  $cronSessions,
  $freshDraftReady,
  $messagingSessions,
  $sessions,
  $sessionsLoading,
  $sessionsTotal,
  setCronSessions,
  setFreshDraftReady,
  setMessagingSessions,
  setSessions,
  setSessionsLoading,
  setSessionsTotal
} from '@/store/session'

import { $gatewaySwitching, wipeSessionListsForGatewaySwitch } from './gateway-switch'

vi.mock('@/lib/query-client', () => ({
  invalidateProfileScopedQueries: vi.fn()
}))

describe('wipeSessionListsForGatewaySwitch', () => {
  beforeEach(() => {
    $gatewaySwitching.set(false)
    setSessions([{ id: 's1', title: 'old', profile: 'default' } as never])
    setSessionsTotal(1)
    setCronSessions([{ id: 'c1', title: 'cron', profile: 'default' } as never])
    setMessagingSessions([{ id: 'm1', title: 'tg', profile: 'default' } as never])
    setSessionsLoading(false)
    setFreshDraftReady(false)
    $sessionsLimit.set(SIDEBAR_SESSIONS_PAGE_SIZE * 3)
  })

  afterEach(() => {
    resetSessionsLimit()
    setSessions([])
    setCronSessions([])
    setMessagingSessions([])
    setSessionsLoading(true)
    $gatewaySwitching.set(false)
  })

  it('clears lists and arms loading so sidebar skeletons retrigger', () => {
    wipeSessionListsForGatewaySwitch()

    expect($sessions.get()).toEqual([])
    expect($sessionsTotal.get()).toBe(0)
    expect($cronSessions.get()).toEqual([])
    expect($messagingSessions.get()).toEqual([])
    expect($sessionsLoading.get()).toBe(true)
    expect($sessionsLimit.get()).toBe(SIDEBAR_SESSIONS_PAGE_SIZE)
    expect($freshDraftReady.get()).toBe(true)
  })

  it('clears every gateway-bound project cache before another profile loads', () => {
    $projects.set([{ id: 'p_default' }] as never)
    $activeProjectId.set('p_default')
    $projectTree.set([{ id: 'p_default' }] as never)
    $projectTreeLoading.set(true)
    $sessionProjectAssignments.set({ shared_session: 'p_default' })
    $projectSessionAssignmentsAvailable.set(true)
    $projectsRpcAvailable.set(true)
    $removedSessionIds.set(new Set(['shared_session']))
    $reposScanning.set(true)
    $projectScope.set('p_default')

    wipeSessionListsForGatewaySwitch()

    expect($projects.get()).toEqual([])
    expect($activeProjectId.get()).toBeNull()
    expect($projectTree.get()).toEqual([])
    expect($projectTreeLoading.get()).toBe(false)
    expect($sessionProjectAssignments.get()).toEqual({})
    expect($projectSessionAssignmentsAvailable.get()).toBeNull()
    expect($projectsRpcAvailable.get()).toBeNull()
    expect($removedSessionIds.get()).toEqual(new Set())
    expect($reposScanning.get()).toBe(false)
    expect($projectScope.get()).toBe(ALL_PROJECTS)
  })
})
