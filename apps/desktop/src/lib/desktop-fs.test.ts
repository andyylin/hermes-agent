import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import {
  desktopDefaultCwd,
  desktopDefaultCwdForProfile,
  desktopFileDiff,
  desktopGitRoot,
  readDesktopDir,
  readDesktopDirForProfile,
  readDesktopFileDataUrl,
  readDesktopFileText,
  selectDesktopPaths,
  selectDesktopPathsForProfile,
  setDesktopFsRemotePicker,
  writeDesktopFileTextForProfile
} from './desktop-fs'

const readDir = vi.fn(async () => ({ entries: [{ name: 'local', path: '/local', isDirectory: true }] }))
const readFileText = vi.fn(async () => ({ path: '/local/file.txt', text: 'local', byteSize: 5 }))
const readFileDataUrl = vi.fn(async () => 'data:text/plain;base64,bG9jYWw=')
const gitRoot = vi.fn(async () => '/local')
const selectPaths = vi.fn(async () => ['/local'])
const writeTextFile = vi.fn(async (path: string) => ({ path }))
const getConnection = vi.fn(async () => ({ mode: 'local' }))

const api = vi.fn(async ({ path }: { path: string }) => {
  if (path.startsWith('/api/fs/list?')) {
    return { entries: [{ name: 'remote', path: '/remote', isDirectory: true }] }
  }

  if (path.startsWith('/api/fs/read-text?')) {
    return { path: '/remote/file.txt', text: 'remote', byteSize: 6 }
  }

  if (path.startsWith('/api/fs/read-data-url?')) {
    return { dataUrl: 'data:text/plain;base64,cmVtb3Rl' }
  }

  if (path.startsWith('/api/fs/git-root?')) {
    return { root: '/remote' }
  }

  if (path === '/api/fs/default-cwd') {
    return { cwd: '/backend/project', branch: 'main' }
  }

  if (path === '/api/fs/write-text') {
    return { ok: true, path: '/remote/IDEA.md' }
  }

  if (path.startsWith('/api/git/file-diff?')) {
    return { diff: 'remote diff' }
  }

  throw new Error(`unexpected path ${path}`)
})

function stubBridge() {
  vi.stubGlobal('window', {
    hermesDesktop: {
      api,
      getConnection,
      gitRoot,
      readDir,
      readFileDataUrl,
      readFileText,
      selectPaths,
      writeTextFile
    }
  })
}

describe('desktop filesystem facade', () => {
  beforeEach(() => {
    stubBridge()
    $connection.set(null)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
    $connection.set(null)
    setDesktopFsRemotePicker(null)
  })

  it('uses local Electron filesystem methods in local mode', async () => {
    $connection.set({ mode: 'local' } as never)

    await expect(readDesktopDir('/work')).resolves.toEqual({
      entries: [{ name: 'local', path: '/local', isDirectory: true }]
    })
    await expect(readDesktopFileText('/work/file.txt')).resolves.toMatchObject({ text: 'local' })
    await expect(readDesktopFileDataUrl('/work/file.txt')).resolves.toBe('data:text/plain;base64,bG9jYWw=')
    await expect(desktopGitRoot('/work')).resolves.toBe('/local')
    await expect(selectDesktopPaths({ directories: true })).resolves.toEqual(['/local'])

    expect(readDir).toHaveBeenCalledWith('/work')
    expect(readFileText).toHaveBeenCalledWith('/work/file.txt')
    expect(readFileDataUrl).toHaveBeenCalledWith('/work/file.txt')
    expect(gitRoot).toHaveBeenCalledWith('/work')
    expect(selectPaths).toHaveBeenCalledWith({ directories: true })
    expect(api).not.toHaveBeenCalled()
  })

  it('routes filesystem reads through authenticated backend REST in remote mode', async () => {
    $connection.set({ mode: 'remote' } as never)

    await expect(readDesktopDir('/home/user/project')).resolves.toMatchObject({ entries: [{ name: 'remote' }] })
    await expect(readDesktopFileText('/home/user/project/a b.txt')).resolves.toMatchObject({ text: 'remote' })
    await expect(readDesktopFileDataUrl('/home/user/project/a b.txt')).resolves.toBe('data:text/plain;base64,cmVtb3Rl')
    await expect(desktopGitRoot('/home/user/project')).resolves.toBe('/remote')
    await expect(desktopDefaultCwd()).resolves.toEqual({ cwd: '/backend/project', branch: 'main' })

    expect(api).toHaveBeenCalledWith({ path: '/api/fs/list?path=%2Fhome%2Fuser%2Fproject' })
    expect(api).toHaveBeenCalledWith({ path: '/api/fs/read-text?path=%2Fhome%2Fuser%2Fproject%2Fa%20b.txt' })
    expect(api).toHaveBeenCalledWith({ path: '/api/fs/read-data-url?path=%2Fhome%2Fuser%2Fproject%2Fa%20b.txt' })
    expect(api).toHaveBeenCalledWith({ path: '/api/fs/git-root?path=%2Fhome%2Fuser%2Fproject' })
    expect(api).toHaveBeenCalledWith({ path: '/api/fs/default-cwd' })
    expect(readDir).not.toHaveBeenCalled()
    expect(readFileText).not.toHaveBeenCalled()
    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(gitRoot).not.toHaveBeenCalled()
  })

  it('targets the active profile backend so a remote profile never reads local disk', async () => {
    $connection.set({ mode: 'remote', profile: 'remote-docker' } as never)

    await readDesktopDir('/srv/project')
    await desktopDefaultCwd()

    expect(api).toHaveBeenCalledWith({ path: '/api/fs/list?path=%2Fsrv%2Fproject', profile: 'remote-docker' })
    expect(api).toHaveBeenCalledWith({ path: '/api/fs/default-cwd', profile: 'remote-docker' })
  })

  it('routes profile-bound picker and writes through the requested profile', async () => {
    const remoteSelect = vi.fn(async () => ['/remote/project'])
    getConnection.mockResolvedValue({ mode: 'remote', profile: 'beta' } as never)
    setDesktopFsRemotePicker({ selectPaths: remoteSelect })

    await expect(desktopDefaultCwdForProfile('beta')).resolves.toEqual({
      branch: 'main',
      cwd: '/backend/project'
    })
    await expect(readDesktopDirForProfile('beta', '/backend/project')).resolves.toMatchObject({
      entries: [{ name: 'remote' }]
    })
    await expect(selectDesktopPathsForProfile('beta', { directories: true, multiple: false })).resolves.toEqual([
      '/remote/project'
    ])
    await expect(writeDesktopFileTextForProfile('beta', '/remote/IDEA.md', '# Idea\n')).resolves.toEqual({
      path: '/remote/IDEA.md'
    })

    expect(getConnection).toHaveBeenCalledWith('beta')
    expect(remoteSelect).toHaveBeenCalledWith({ directories: true, multiple: false }, 'beta')
    expect(api).toHaveBeenCalledWith({ path: '/api/fs/list?path=%2Fbackend%2Fproject', profile: 'beta' })
    expect(api).toHaveBeenCalledWith({ path: '/api/fs/default-cwd', profile: 'beta' })
    expect(api).toHaveBeenCalledWith({
      body: { content: '# Idea\n', path: '/remote/IDEA.md' },
      method: 'POST',
      path: '/api/fs/write-text',
      profile: 'beta'
    })
    expect(writeTextFile).not.toHaveBeenCalled()
  })

  it('routes file diffs through backend git in remote mode', async () => {
    $connection.set({ mode: 'remote' } as never)

    await expect(desktopFileDiff('/repo', 'src/a b.ts')).resolves.toBe('remote diff')
    expect(api).toHaveBeenCalledWith({ path: '/api/git/file-diff?path=%2Frepo&file=src%2Fa%20b.ts' })
  })

  it('uses the registered in-app directory picker in remote mode', async () => {
    const remoteSelect = vi.fn(async () => ['/remote/project'])
    $connection.set({ mode: 'remote' } as never)
    setDesktopFsRemotePicker({ selectPaths: remoteSelect })

    await expect(selectDesktopPaths({ defaultPath: '/remote', directories: true, multiple: false })).resolves.toEqual([
      '/remote/project'
    ])

    expect(remoteSelect).toHaveBeenCalledWith({ defaultPath: '/remote', directories: true, multiple: false })
    expect(selectPaths).not.toHaveBeenCalled()
  })

  it('uses the local Electron picker for remote file selection', async () => {
    const remoteSelect = vi.fn(async () => ['/remote/project'])
    $connection.set({ mode: 'remote' } as never)
    setDesktopFsRemotePicker({ selectPaths: remoteSelect })

    await expect(selectDesktopPaths({ directories: false, multiple: false })).resolves.toEqual(['/local'])

    expect(selectPaths).toHaveBeenCalledWith({ directories: false, multiple: false })
    expect(remoteSelect).not.toHaveBeenCalled()
  })

  it('limits the remote picker to single-directory selection', async () => {
    const remoteSelect = vi.fn(async () => ['/remote/project'])
    $connection.set({ mode: 'remote' } as never)
    setDesktopFsRemotePicker({ selectPaths: remoteSelect })

    await expect(selectDesktopPaths({ directories: true })).resolves.toEqual(['/remote/project'])

    expect(remoteSelect).toHaveBeenCalledWith({ directories: true, multiple: false })
    expect(selectPaths).not.toHaveBeenCalled()
  })
})
