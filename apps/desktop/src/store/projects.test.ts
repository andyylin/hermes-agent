import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import { $sidebarAgentsGrouped } from '@/store/layout'
import { $activeGatewayProfile } from '@/store/profile'
import type { ProjectInfo } from '@/types/hermes'

import {
  $activeProjectId,
  $projectDialog,
  $projects,
  $projectScope,
  $projectsRpcAvailable,
  $projectTree,
  $projectTreeLoading,
  $removedSessionIds,
  $worktreeRefreshToken,
  ALL_PROJECTS,
  createProject,
  enterProject,
  exitProjectScope,
  openProjectCreate,
  pickProjectFolder,
  projectNameForCwd,
  refreshProjects,
  refreshProjectTree,
  refreshWorktrees,
  scanAndRecordRepos,
  tombstoneSessions,
  updateProject
} from './projects'

const { scanRepos } = vi.hoisted(() => ({ scanRepos: vi.fn() }))

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

vi.mock('@/lib/desktop-git', () => ({
  desktopGit: () => ({ scanRepos })
}))

vi.mock('@/store/gateway', () => ({
  activeGateway: vi.fn(),
  ensureActiveGatewayOpen: vi.fn()
}))

const fs = await import('@/lib/desktop-fs')
const desktopDefaultCwd = vi.mocked(fs.desktopDefaultCwd)
const isDesktopFsRemoteMode = vi.mocked(fs.isDesktopFsRemoteMode)
const selectDesktopPaths = vi.mocked(fs.selectDesktopPaths)

const gw = await import('@/store/gateway')
const activeGateway = vi.mocked(gw.activeGateway)
const notifications = await import('@/store/notifications')
const notify = vi.mocked(notifications.notify)

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void

  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, reject, resolve }
}

const project = (id: string, name: string): ProjectInfo => ({
  archived: false,
  board_slug: null,
  color: null,
  created_at: 0,
  description: null,
  folders: [],
  icon: null,
  id,
  name,
  primary_path: null,
  slug: id
})

describe('profile isolation', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    $activeGatewayProfile.set('default')
    $projects.set([])
    $projectScope.set(ALL_PROJECTS)
  })

  it('drops a stale projects.list response after switching profiles', async () => {
    const responseA = deferred<{ active_id: null; projects: ProjectInfo[] }>()
    const gatewayA = { connectionState: 'open', request: vi.fn(() => responseA.promise) }
    const gatewayB = { connectionState: 'open', request: vi.fn() }

    $activeGatewayProfile.set('alpha')
    activeGateway.mockReturnValue(gatewayA as never)
    const refresh = refreshProjects()

    $activeGatewayProfile.set('beta')
    activeGateway.mockReturnValue(gatewayB as never)
    responseA.resolve({ active_id: null, projects: [project('p_alpha', 'Alpha')] })
    await refresh

    expect($projects.get()).toEqual([])
  })

  it('drops an old alpha response after a rapid alpha to beta to alpha swap', async () => {
    const oldAlpha = deferred<{ active_id: null; projects: ProjectInfo[] }>()
    const gatewayA = { connectionState: 'open', request: vi.fn(() => oldAlpha.promise) }
    const gatewayB = { connectionState: 'open', request: vi.fn() }

    $activeGatewayProfile.set('rapid-alpha')
    activeGateway.mockReturnValue(gatewayA as never)
    const refresh = refreshProjects()

    $activeGatewayProfile.set('rapid-beta')
    activeGateway.mockReturnValue(gatewayB as never)
    $activeGatewayProfile.set('rapid-alpha')
    activeGateway.mockReturnValue(gatewayA as never)
    $projects.set([project('p_fresh', 'Fresh alpha')])

    oldAlpha.resolve({ active_id: null, projects: [project('p_stale', 'Stale alpha')] })
    await refresh

    expect($projects.get().map(item => item.id)).toEqual(['p_fresh'])
  })

  it('never submits alpha repo scan results through beta gateway', async () => {
    const scanned = deferred<Array<{ path: string }>>()
    const gatewayA = { connectionState: 'open', request: vi.fn().mockResolvedValue({}) }
    const gatewayB = { connectionState: 'open', request: vi.fn().mockResolvedValue({}) }
    scanRepos.mockReturnValue(scanned.promise)

    $activeGatewayProfile.set('scan-alpha')
    activeGateway.mockReturnValue(gatewayA as never)
    const scan = scanAndRecordRepos(true)

    $activeGatewayProfile.set('scan-beta')
    activeGateway.mockReturnValue(gatewayB as never)
    scanned.resolve([{ path: '/repos/alpha' }])
    await scan

    expect(gatewayB.request).not.toHaveBeenCalledWith('projects.record_repos', expect.anything())
  })

  it('scans once for each profile instead of suppressing beta after alpha', async () => {
    const gatewayA = { connectionState: 'open', request: vi.fn().mockResolvedValue({}) }
    const gatewayB = { connectionState: 'open', request: vi.fn().mockResolvedValue({}) }
    scanRepos.mockResolvedValue([{ path: '/repos/shared' }])

    $activeGatewayProfile.set('once-alpha')
    activeGateway.mockReturnValue(gatewayA as never)
    await scanAndRecordRepos()

    $activeGatewayProfile.set('once-beta')
    activeGateway.mockReturnValue(gatewayB as never)
    await scanAndRecordRepos()

    expect(gatewayA.request).toHaveBeenCalledWith('projects.record_repos', expect.anything())
    expect(gatewayB.request).toHaveBeenCalledWith('projects.record_repos', expect.anything())
  })

  it('does not roll an alpha optimistic snapshot into beta after a failed write', async () => {
    const writeA = deferred<never>()
    const gatewayA = { connectionState: 'open', request: vi.fn(() => writeA.promise) }

    $activeGatewayProfile.set('write-alpha')
    activeGateway.mockReturnValue(gatewayA as never)
    $projects.set([project('p_alpha', 'Alpha')])
    const update = updateProject('p_alpha', { name: 'Alpha renamed' })
    await Promise.resolve()

    $activeGatewayProfile.set('write-beta')
    $projects.set([project('p_beta', 'Beta')])
    writeA.reject(new Error('alpha write failed'))
    await expect(update).rejects.toThrow()

    expect($projects.get().map(project => project.id)).toEqual(['p_beta'])
  })

  it('does not mutate beta after switching during the context-capture microtask', async () => {
    const gatewayA = { connectionState: 'open', request: vi.fn().mockResolvedValue({}) }

    $activeGatewayProfile.set('microtask-alpha')
    activeGateway.mockReturnValue(gatewayA as never)
    $projects.set([project('p_shared', 'Alpha')])
    const update = updateProject('p_shared', { name: 'Alpha renamed' })

    $activeGatewayProfile.set('microtask-beta')
    $projects.set([project('p_shared', 'Beta')])
    await expect(update).rejects.toThrow()

    expect($projects.get()[0]?.name).toBe('Beta')
  })

  it('does not leave beta loading when switching during tree context capture', async () => {
    const gatewayA = { connectionState: 'open', request: vi.fn().mockResolvedValue({}) }

    $activeGatewayProfile.set('loading-alpha')
    activeGateway.mockReturnValue(gatewayA as never)
    const refresh = refreshProjectTree()

    $activeGatewayProfile.set('loading-beta')
    $projectTreeLoading.set(false)
    await refresh

    expect($projectTreeLoading.get()).toBe(false)
  })

  it('persists project scope independently per profile', () => {
    $activeGatewayProfile.set('scope-alpha')
    enterProject('p_alpha')

    $activeGatewayProfile.set('scope-beta')
    expect($projectScope.get()).toBe(ALL_PROJECTS)
    enterProject('p_beta')

    $activeGatewayProfile.set('scope-alpha')
    expect($projectScope.get()).toBe('p_alpha')
  })

  it('clears every profile-bound project view atom during a switch', () => {
    $activeGatewayProfile.set('reset-alpha')
    $projects.set([project('p_alpha', 'Alpha')])
    $activeProjectId.set('p_alpha')
    $projectTreeLoading.set(true)
    openProjectCreate()
    $projectsRpcAvailable.set(false)
    tombstoneSessions(['s_alpha'])

    $activeGatewayProfile.set('reset-beta')

    expect($projects.get()).toEqual([])
    expect($activeProjectId.get()).toBeNull()
    expect($projectTreeLoading.get()).toBe(false)
    expect($projectsRpcAvailable.get()).toBeNull()
    expect($removedSessionIds.get().size).toBe(0)
    expect($projectDialog.get()).toBeNull()
  })
})

describe('project scope', () => {
  beforeEach(() => {
    window.localStorage.clear()
    $activeGatewayProfile.set('default')
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
    expect(window.localStorage.getItem('hermes.desktop.projectScope.default')).toBe('p_abc')
  })
})

describe('projectNameForCwd', () => {
  const treeNode = (
    over: Partial<SidebarProjectTree> & Pick<SidebarProjectTree, 'id' | 'label'>
  ): SidebarProjectTree => ({
    path: null,
    repos: [],
    sessionCount: 0,
    ...over
  })

  beforeEach(() => {
    $projectTree.set([])
  })

  it('names the explicit project owning the cwd (longest path match)', () => {
    $projectTree.set([
      treeNode({ id: 'p_web', label: 'Website', path: '/repos/website' }),
      treeNode({ id: 'p_api', label: 'API', path: '/repos/api' })
    ])

    expect(projectNameForCwd('/repos/website/src/app')).toBe('Website')
  })

  it('matches nested repo and worktree paths, not just the project root', () => {
    $projectTree.set([
      treeNode({
        id: 'p_mono',
        label: 'Monorepo',
        path: '/repos/mono',
        repos: [
          {
            id: 'r1',
            label: 'mono',
            path: '/repos/mono',
            sessionCount: 0,
            groups: [{ id: 'g1', label: 'feature', path: '/elsewhere/mono-feature', sessions: [] }]
          }
        ]
      })
    ])

    // A linked worktree lives OUTSIDE the project root but still belongs to it.
    expect(projectNameForCwd('/elsewhere/mono-feature/src')).toBe('Monorepo')
  })

  it('ignores auto-projects and the No-project bucket (no named identity)', () => {
    $projectTree.set([
      treeNode({ id: '/repos/loose', label: 'loose', path: '/repos/loose', isAuto: true }),
      treeNode({ id: '__no_project__', label: 'No project', path: null, isNoProject: true })
    ])

    expect(projectNameForCwd('/repos/loose/src')).toBeNull()
  })

  it('returns null for a cwd in no project and for a blank cwd', () => {
    $projectTree.set([treeNode({ id: 'p_web', label: 'Website', path: '/repos/website' })])

    expect(projectNameForCwd('/somewhere/else')).toBeNull()
    expect(projectNameForCwd('')).toBeNull()
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
  beforeEach(() => {
    vi.clearAllMocks()
    $sidebarAgentsGrouped.set(false)
    $activeProjectId.set(null)
    $projectsRpcAvailable.set(null)
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
})
