import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile, $activeGatewayProfileGeneration } from '@/store/profile'
import { $connection } from '@/store/session'

import {
  desktopGit,
  desktopGitForProfile,
  scanDesktopReposForProfile,
  StaleDesktopGitProfileError
} from './desktop-git'

const repoStatus = vi.fn(async () => ({ branch: 'main' }))
const scanRepos = vi.fn(async () => [{ label: 'Repo', root: '/repo' }])
const worktreeList = vi.fn(async () => [{ branch: 'main', detached: false, isMain: true, locked: false, path: '/r' }])
const localGit = { repoStatus, review: { stage: vi.fn() }, scanRepos, worktreeList }
const getConnection = vi.fn(async () => ({ mode: 'local' }))

const api = vi.fn(async ({ path }: { path: string }) => {
  if (path.startsWith('/api/git/status')) {
    return { branch: 'remote-main' }
  }

  if (path.startsWith('/api/git/worktrees')) {
    return { worktrees: [{ branch: 'main', detached: false, isMain: true, locked: false, path: '/srv/r' }] }
  }

  if (path.startsWith('/api/git/review/diff')) {
    return { diff: 'remote-diff' }
  }

  return { ok: true }
})

describe('desktop git facade', () => {
  beforeEach(() => {
    vi.stubGlobal('window', { hermesDesktop: { api, getConnection, git: localGit } })
    $activeGatewayProfile.set('default')
    $connection.set(null)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
    $connection.set(null)
  })

  it('uses Electron git locally', async () => {
    $connection.set({ mode: 'local' } as never)

    await expect(desktopGit()?.repoStatus('/work')).resolves.toEqual({ branch: 'main' })
    expect(repoStatus).toHaveBeenCalledWith('/work')
    expect(api).not.toHaveBeenCalled()
  })

  it('routes reads through the backend REST mirror on a remote gateway', async () => {
    $connection.set({ mode: 'remote' } as never)

    await expect(desktopGit()?.repoStatus('/srv/work')).resolves.toEqual({ branch: 'remote-main' })
    expect(api).toHaveBeenCalledWith({ path: '/api/git/status?path=%2Fsrv%2Fwork' })

    // List endpoints unwrap their envelope to the bare array the bridge returns.
    await expect(desktopGit()?.worktreeList('/srv/work')).resolves.toEqual([
      { branch: 'main', detached: false, isMain: true, locked: false, path: '/srv/r' }
    ])

    // review.diff unwraps { diff } to a string.
    await expect(desktopGit()?.review.diff('/srv/work', 'a.txt', 'uncommitted', null, false)).resolves.toBe(
      'remote-diff'
    )

    expect(repoStatus).not.toHaveBeenCalled()
  })

  it('targets the active profile backend so a remote profile never touches the local repo', async () => {
    $activeGatewayProfile.set('remote-docker')
    $connection.set({ mode: 'remote', profile: 'remote-docker' } as never)

    await desktopGit()?.repoStatus('/srv/work')
    await desktopGit()?.review.stage('/srv/work', 'a.txt')

    expect(api).toHaveBeenCalledWith({ path: '/api/git/status?path=%2Fsrv%2Fwork', profile: 'remote-docker' })
    expect(api).toHaveBeenCalledWith({
      body: { file: 'a.txt', path: '/srv/work' },
      method: 'POST',
      path: '/api/git/review/stage',
      profile: 'remote-docker'
    })
  })

  it('sends mutations as POST bodies on a remote gateway', async () => {
    $connection.set({ mode: 'remote' } as never)

    await desktopGit()?.review.stage('/srv/work', 'a.txt')

    expect(api).toHaveBeenCalledWith({
      body: { file: 'a.txt', path: '/srv/work' },
      method: 'POST',
      path: '/api/git/review/stage'
    })
    expect(localGit.review.stage).not.toHaveBeenCalled()
  })

  it('binds repo discovery to the requested profile connection', async () => {
    $activeGatewayProfile.set('alpha')
    const alphaGeneration = $activeGatewayProfileGeneration.get()

    getConnection.mockResolvedValueOnce({ mode: 'local', profile: 'alpha' } as never)

    await expect(scanDesktopReposForProfile('alpha', alphaGeneration)).resolves.toEqual([
      { label: 'Repo', root: '/repo' }
    ])
    expect(getConnection).toHaveBeenCalledWith('alpha')
    expect(scanRepos).toHaveBeenCalledWith([])

    $activeGatewayProfile.set('beta')
    const betaGeneration = $activeGatewayProfileGeneration.get()

    getConnection.mockResolvedValueOnce({ mode: 'remote', profile: 'beta' } as never)
    await expect(scanDesktopReposForProfile('beta', betaGeneration)).resolves.toEqual([])
    expect(scanRepos).toHaveBeenCalledTimes(1)
  })

  it('binds remote git operations to the requested profile instead of the foreground connection', async () => {
    $activeGatewayProfile.set('remote-beta')
    const generation = $activeGatewayProfileGeneration.get()

    $connection.set({ mode: 'local', profile: 'foreground-alpha' } as never)
    getConnection.mockResolvedValueOnce({ mode: 'remote', profile: 'remote-beta' } as never)

    const git = await desktopGitForProfile('remote-beta', generation)
    await git?.repoStatus('/srv/work')

    expect(getConnection).toHaveBeenCalledWith('remote-beta')
    expect(api).toHaveBeenCalledWith({
      path: '/api/git/status?path=%2Fsrv%2Fwork',
      profile: 'remote-beta'
    })
    expect(repoStatus).not.toHaveBeenCalled()
  })

  it('classifies a delayed Git completion after a profile switch as stale cancellation', async () => {
    let resolveStatus!: (value: { branch: string }) => void

    repoStatus.mockImplementationOnce(() => new Promise(resolve => (resolveStatus = resolve)))
    $activeGatewayProfile.set('alpha')
    const generation = $activeGatewayProfileGeneration.get()
    getConnection.mockResolvedValueOnce({ mode: 'local', profile: 'alpha' } as never)
    const git = await desktopGitForProfile('alpha', generation)
    const pending = git?.repoStatus('/work')

    $activeGatewayProfile.set('beta')
    resolveStatus({ branch: 'main' })

    await expect(pending).rejects.toBeInstanceOf(StaleDesktopGitProfileError)
  })
})
