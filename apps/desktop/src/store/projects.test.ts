import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $sidebarAgentsGrouped } from '@/store/layout'

import {
  $activeProjectId,
  $managedProjectCreateAvailable,
  $projectDialog,
  $projects,
  $projectScope,
  $projectSessionAssignmentsAvailable,
  $projectsRpcAvailable,
  $sessionProjectAssignments,
  $worktreeRefreshToken,
  ALL_PROJECTS,
  assignSessionToProject,
  createManagedProject,
  createProject,
  enterProject,
  exitProjectScope,
  openProjectCreate,
  pickProjectFolder,
  refreshProjects,
  refreshProjectTree,
  refreshWorktrees,
  unassignSessionFromProject
} from './projects'


vi.mock('@/i18n', () => ({
  translateNow: (key: string) => key
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn()
}))

vi.mock('@/lib/desktop-fs', () => ({
  desktopDefaultCwd: vi.fn(),
  isDesktopFsRemoteMode: vi.fn(),
  selectDesktopPaths: vi.fn(),
  writeDesktopFileText: vi.fn()
}))

vi.mock('@/store/gateway', () => ({
  $activeGatewayProfile: atom('default'),
  activeGateway: vi.fn(),
  ensureActiveGatewayOpen: vi.fn()
}))

const fs = await import('@/lib/desktop-fs')
const desktopDefaultCwd = vi.mocked(fs.desktopDefaultCwd)
const isDesktopFsRemoteMode = vi.mocked(fs.isDesktopFsRemoteMode)
const selectDesktopPaths = vi.mocked(fs.selectDesktopPaths)

const gw = await import('@/store/gateway')
const activeGatewayProfile = gw.$activeGatewayProfile
const activeGateway = vi.mocked(gw.activeGateway)
const notifications = await import('@/store/notifications')
const notify = vi.mocked(notifications.notify)

describe('project scope', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $projectScope.set(ALL_PROJECTS)
  })

  it('defaults to ALL_PROJECTS', () => {
    expect($projectScope.get()).toBe(ALL_PROJECTS)
  })

  it('enterProject scopes the sidebar to the project id', () => {
    // setActiveProject fires best-effort (no gateway in test → it rejects and is
    // swallowed); the synchronous scope change is what matters here.
    enterProject('p_123')
    expect($projectScope.get()).toBe('p_123')
  })

  it('exitProjectScope returns to the overview', () => {
    enterProject('p_123')
    exitProjectScope()
    expect($projectScope.get()).toBe(ALL_PROJECTS)
  })

  it('entering the synthetic No-project bucket still scopes (no active pin)', () => {
    enterProject('__no_project__')
    expect($projectScope.get()).toBe('__no_project__')
  })

  it('persists the scope to localStorage', () => {
    enterProject('p_abc')
    expect(window.localStorage.getItem('hermes.desktop.projectScope')).toBe('p_abc')
  })
})

describe('worktree refresh', () => {
  it('refreshWorktrees bumps the probe token so useRepoWorktreeMap refetches', () => {
    const before = $worktreeRefreshToken.get()
    refreshWorktrees()
    expect($worktreeRefreshToken.get()).toBe(before + 1)
  })
})

describe('pickProjectFolder', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('uses the remote-aware directory picker locally', async () => {
    isDesktopFsRemoteMode.mockReturnValue(false)
    selectDesktopPaths.mockResolvedValue(['/local/repo'])

    await expect(pickProjectFolder()).resolves.toBe('/local/repo')
    expect(selectDesktopPaths).toHaveBeenCalledWith({ defaultPath: undefined, directories: true, multiple: false })
  })

  it('seeds the picker with the backend cwd on a remote gateway', async () => {
    isDesktopFsRemoteMode.mockReturnValue(true)
    desktopDefaultCwd.mockResolvedValue({ branch: 'main', cwd: '/backend/work' })
    selectDesktopPaths.mockResolvedValue(['/backend/work/repo'])

    await expect(pickProjectFolder()).resolves.toBe('/backend/work/repo')
    expect(selectDesktopPaths).toHaveBeenCalledWith({
      defaultPath: '/backend/work',
      directories: true,
      multiple: false
    })
  })

  it('returns null when the picker is cancelled (empty selection)', async () => {
    isDesktopFsRemoteMode.mockReturnValue(false)
    selectDesktopPaths.mockResolvedValue([])

    await expect(pickProjectFolder()).resolves.toBeNull()
  })
})

describe('createProject', () => {
  let profileNumber = 0

  beforeEach(() => {
    vi.clearAllMocks()
    $sidebarAgentsGrouped.set(false)
    $activeProjectId.set(null)
    $projectsRpcAvailable.set(null)
    $managedProjectCreateAvailable.set(null)
    $projects.set([])
    $projectDialog.set(null)
    profileNumber += 1
    activeGatewayProfile.set(`test-${profileNumber}`)
  })

  it('creates the project and flips into the grouped view so a blank slate shows it', async () => {
    const created = { folders: [], id: 'p_new', name: 'Demo', primary_path: '/srv/demo' }

    const request = vi.fn(async (method: string) => {
      if (method === 'projects.create') {
        return { project: created }
      }

      // Reconcile (fire-and-forget) re-reads list + tree; echo the project back
      // so the optimistic state survives instead of being wiped to empty.
      return { active_id: 'p_new', projects: [created], scoped_session_ids: [] }
    })

    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    const result = await createProject({ folders: ['/srv/demo'], name: 'Demo', use: true })

    expect(result).toEqual(created)
    expect(request).toHaveBeenCalledWith('projects.create', expect.objectContaining({ name: 'Demo' }))
    expect($sidebarAgentsGrouped.get()).toBe(true)
    expect($activeProjectId.get()).toBe('p_new')
  })

  it('marks the backend stale and surfaces a friendly error when projects.create is missing', async () => {
    activeGateway.mockReturnValue({
      connectionState: 'open',
      request: vi.fn().mockRejectedValue(new Error('unknown method: projects.create'))
    } as never)

    await expect(createProject({ folders: ['/srv/demo'], name: 'Demo' })).rejects.toThrow(
      'sidebar.projects.staleBackend'
    )
    expect($projectsRpcAvailable.get()).toBe(false)
  })

  it('creates a managed project without sending a client-selected folder', async () => {
    const created = {
      folders: [{ path: '/profile/projects/demo' }],
      id: 'p_managed',
      name: 'Demo',
      primary_path: '/profile/projects/demo'
    }
    const request = vi.fn(async (method: string) => {
      if (method === 'projects.create_managed') {
        return { project: created }
      }
      return { active_id: 'p_managed', projects: [created], scoped_session_ids: [] }
    })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    const result = await createManagedProject({ name: 'Demo', use: true })

    expect(result).toEqual(created)
    expect(request).toHaveBeenCalledWith(
      'projects.create_managed',
      expect.not.objectContaining({ folders: expect.anything(), primary_path: expect.anything() })
    )
    expect($activeProjectId.get()).toBe('p_managed')
  })

  it('does not mark all Projects stale when only managed creation is unsupported', async () => {
    const unsupportedProfile = activeGatewayProfile.get()
    const unsupportedGateway = {
      connectionState: 'open',
      request: vi.fn().mockRejectedValue(new Error('unknown method: projects.create_managed'))
    } as never
    activeGateway.mockReturnValue(unsupportedGateway)

    await expect(createManagedProject({ name: 'Demo' })).rejects.toThrow('sidebar.projects.managedUnsupported')
    expect($projectsRpcAvailable.get()).not.toBe(false)
    expect($managedProjectCreateAvailable.get()).toBe(false)

    openProjectCreate()
    expect($projectDialog.get()).toEqual({ folderMode: 'existing', mode: 'create' })

    const newerGateway = { connectionState: 'open', request: vi.fn() } as never
    activeGateway.mockReturnValue(newerGateway)
    activeGatewayProfile.set('newer-profile')
    expect($managedProjectCreateAvailable.get()).toBeNull()
    activeGateway.mockReturnValue(unsupportedGateway)
    activeGatewayProfile.set(unsupportedProfile)
    expect($managedProjectCreateAvailable.get()).toBe(false)
  })

  it('does not apply a completed create to a profile selected while the request was in flight', async () => {
    let resolveCreate: ((value: unknown) => void) | undefined
    const request = vi.fn(
      () =>
        new Promise(resolve => {
          resolveCreate = resolve
        })
    )
    const profileAGateway = { connectionState: 'open', request } as never
    activeGateway.mockReturnValue(profileAGateway)

    const creating = createManagedProject({ idea: 'Keep this in A', name: 'Profile A', use: true })
    await vi.waitFor(() => expect(resolveCreate).toBeTypeOf('function'))

    activeGateway.mockReturnValue({ connectionState: 'open', request: vi.fn() } as never)
    activeGatewayProfile.set('profile-b')
    resolveCreate?.({
      project: {
        folders: [{ path: '/profile-a/projects/a' }],
        id: 'p_a',
        name: 'Profile A',
        primary_path: '/profile-a/projects/a'
      }
    })

    await expect(creating).resolves.toEqual(expect.objectContaining({ id: 'p_a' }))
    expect($projects.get()).toEqual([])
    expect($activeProjectId.get()).toBeNull()
  })
})

describe('projects RPC capability', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $projectsRpcAvailable.set(null)
  })

  it('marks the backend stale when projects.list is missing', async () => {
    activeGateway.mockReturnValue({
      connectionState: 'open',
      request: vi.fn().mockRejectedValue(new Error('unknown method: projects.list'))
    } as never)

    await refreshProjects()

    expect($projectsRpcAvailable.get()).toBe(false)
  })

  it('blocks opening the create dialog once the backend is known stale', () => {
    $projectsRpcAvailable.set(false)

    openProjectCreate()

    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'warning', message: 'sidebar.projects.staleBackend' })
    )
  })

  it('detects an older project backend without session assignments', async () => {
    activeGateway.mockReturnValue({
      connectionState: 'open',
      request: vi.fn().mockResolvedValue({ active_id: null, projects: [], scoped_session_ids: [] })
    } as never)
    $projectSessionAssignmentsAvailable.set(null)

    await refreshProjectTree()

    expect($projectSessionAssignmentsAvailable.get()).toBe(false)
  })
})

describe('project session assignment cache', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    $sessionProjectAssignments.set({ existing: 'p_old' })
    $projectSessionAssignmentsAvailable.set(true)
  })

  it('rolls back the optimistic assignment when the write fails', async () => {
    activeGateway.mockReturnValue({
      connectionState: 'open',
      request: vi.fn().mockRejectedValue(new Error('write failed'))
    } as never)

    const write = assignSessionToProject('s1', 'p_new')
    expect($sessionProjectAssignments.get()).toEqual({ existing: 'p_old', s1: 'p_new' })
    await expect(write).rejects.toThrow('write failed')
    expect($sessionProjectAssignments.get()).toEqual({ existing: 'p_old' })
  })

  it('rolls back one failed session without clobbering a newer session move', async () => {
    let rejectFirst: ((error: Error) => void) | undefined
    const request = vi.fn((method: string, params?: { session_id?: string }) => {
      if (method !== 'projects.assign_session') {
        return Promise.resolve({
          active_id: null,
          projects: [],
          scoped_session_ids: [],
          session_project_assignments: { s2: 'p_two' }
        })
      }
      if (params?.session_id === 's1') {
        return new Promise((_resolve, reject) => {
          rejectFirst = reject
        })
      }
      return Promise.resolve({})
    })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    const first = assignSessionToProject('s1', 'p_one')
    const second = assignSessionToProject('s2', 'p_two')
    await vi.waitFor(() => expect(rejectFirst).toBeTypeOf('function'))
    rejectFirst?.(new Error('first failed'))

    await expect(first).rejects.toThrow('first failed')
    await second
    expect($sessionProjectAssignments.get()).toEqual({ s2: 'p_two' })
  })

  it('returns to the confirmed value when two overlapping moves both fail', async () => {
    activeGateway.mockReturnValue({
      connectionState: 'open',
      request: vi.fn().mockRejectedValue(new Error('write failed'))
    } as never)

    const first = assignSessionToProject('same', 'p_one')
    const second = assignSessionToProject('same', 'p_two')

    await Promise.allSettled([first, second])
    expect($sessionProjectAssignments.get()).toEqual({ existing: 'p_old' })
  })

  it('rejects a stale tree that finishes after the latest queued move', async () => {
    let resolveFirst: ((value: unknown) => void) | undefined
    let resolveSecond: ((value: unknown) => void) | undefined
    let resolveStaleTree: ((value: unknown) => void) | undefined
    let assignmentCalls = 0
    let treeCalls = 0
    const request = vi.fn((method: string) => {
      if (method === 'projects.assign_session') {
        assignmentCalls += 1
        return new Promise(resolve => {
          if (assignmentCalls === 1) {
            resolveFirst = resolve
          } else {
            resolveSecond = resolve
          }
        })
      }
      if (method === 'projects.tree') {
        treeCalls += 1

        if (treeCalls === 1) {
          return new Promise(resolve => {
            resolveStaleTree = resolve
          })
        }

        return Promise.resolve({
          active_id: null,
          projects: [],
          scoped_session_ids: ['same'],
          session_project_assignments: { same: 'p_two' }
        })
      }
      return Promise.resolve({ projects: [] })
    })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    const first = assignSessionToProject('same', 'p_one')
    const second = assignSessionToProject('same', 'p_two')
    expect($sessionProjectAssignments.get().same).toBe('p_two')

    const staleRefresh = refreshProjectTree()
    await vi.waitFor(() => expect(resolveStaleTree).toBeTypeOf('function'))
    await vi.waitFor(() => expect(resolveFirst).toBeTypeOf('function'))
    resolveFirst?.({})
    await first
    await vi.waitFor(() => expect(resolveSecond).toBeTypeOf('function'))

    resolveSecond?.({})
    await second

    resolveStaleTree?.({
      active_id: null,
      projects: [],
      scoped_session_ids: ['same'],
      session_project_assignments: { same: 'p_one' }
    })
    await staleRefresh
    expect($sessionProjectAssignments.get().same).toBe('p_two')

    activeGateway.mockReturnValue({
      connectionState: 'open',
      request: vi.fn().mockRejectedValue(new Error('later write failed'))
    } as never)
    await expect(assignSessionToProject('same', 'p_three')).rejects.toThrow('later write failed')
    expect($sessionProjectAssignments.get().same).toBe('p_two')
  })

  it('keeps the optimistic result when reconciliation transiently fails', async () => {
    const request = vi.fn(async (method: string) => {
      if (method === 'projects.unassign_session') {
        return {}
      }
      throw new Error('refresh unavailable')
    })
    activeGateway.mockReturnValue({ connectionState: 'open', request } as never)

    await unassignSessionFromProject('s1')

    expect($sessionProjectAssignments.get()).toEqual({ existing: 'p_old', s1: null })
    expect($projectSessionAssignmentsAvailable.get()).toBe(true)
  })
})
