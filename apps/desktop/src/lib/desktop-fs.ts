import type {
  HermesConnection,
  HermesReadDirResult,
  HermesReadFileTextResult,
  HermesSelectPathsOptions
} from '@/global'
import {
  activeGatewayProfileContextIsCurrent,
  captureActiveGatewayProfileContext,
  normalizeProfileKey
} from '@/store/profile'
import { $connection } from '@/store/session'

export interface DesktopFsRemotePicker {
  selectPaths: (options?: HermesSelectPathsOptions, profile?: string, generation?: number) => Promise<string[]>
}

let remotePicker: DesktopFsRemotePicker | null = null

function foregroundConnection(): HermesConnection | null {
  const connection = $connection.get()
  const activeProfile = captureActiveGatewayProfileContext().profile
  const connectionProfile = normalizeProfileKey(connection?.profile)

  if (connectionProfile !== activeProfile) {
    throw new Error(
      `Desktop filesystem connection profile ${connectionProfile} does not match active gateway profile ${activeProfile}`
    )
  }

  return connection
}

export function setDesktopFsRemotePicker(next: DesktopFsRemotePicker | null) {
  remotePicker = next
}

function connectionCacheKey(connection: HermesConnection | null) {
  if (!connection) {
    return 'local:'
  }

  return `${connection.mode || 'local'}:${connection.profile || ''}:${connection.baseUrl || ''}`
}

export function desktopFsCacheKey() {
  return connectionCacheKey($connection.get())
}

export function isDesktopFsRemoteMode() {
  return foregroundConnection()?.mode === 'remote'
}

// Active profile for FS/git REST calls. Without it the Electron api bridge
// hits the primary (local) backend even when the user switched to a remote profile.
export function desktopFsProfile(): string | undefined {
  return $connection.get()?.profile || undefined
}

function fsPath(endpoint: string, filePath: string) {
  return `/api/fs/${endpoint}?path=${encodeURIComponent(filePath)}`
}

function bridge() {
  const desktop = window.hermesDesktop

  if (!desktop) {
    throw new Error('Hermes Desktop bridge is unavailable')
  }

  return desktop
}

function remoteFsApi<T>(path: string, body?: Record<string, unknown>): Promise<T> {
  return bridge().api<T>(
    body ? { body, method: 'POST', path, profile: desktopFsProfile() } : { path, profile: desktopFsProfile() }
  )
}

function remoteFsApiForProfile<T>(profile: string, path: string, body?: Record<string, unknown>): Promise<T> {
  return bridge().api<T>(body ? { body, method: 'POST', path, profile } : { path, profile })
}

function fsContext(profile: string, generation: number) {
  return { generation, profile: normalizeProfileKey(profile) }
}

function assertFsContext(profile: string, generation: number, operation: string) {
  if (!activeGatewayProfileContextIsCurrent(fsContext(profile, generation))) {
    throw new Error(`Desktop filesystem profile ownership changed ${operation}`)
  }
}

async function connectionForFsContext(profile: string, generation: number) {
  assertFsContext(profile, generation, 'before resolving connection')
  const connection = await bridge().getConnection(profile)

  assertFsContext(profile, generation, 'while resolving connection')

  return connection
}

export async function readDesktopDir(path: string): Promise<HermesReadDirResult> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readDir(path)
  }

  return remoteFsApi<HermesReadDirResult>(fsPath('list', path))
}

export async function readDesktopDirForProfile(
  profile: string,
  generation: number,
  path: string
): Promise<HermesReadDirResult> {
  const context = { generation, profile: normalizeProfileKey(profile) }

  if (!activeGatewayProfileContextIsCurrent(context)) {
    const current = captureActiveGatewayProfileContext()

    throw new Error(
      `Desktop filesystem profile ownership changed before directory read (expected ${context.profile}@${context.generation}, current ${current.profile}@${current.generation})`
    )
  }

  const connection = await bridge().getConnection(profile)

  if (!activeGatewayProfileContextIsCurrent(context)) {
    throw new Error('Desktop filesystem profile ownership changed while resolving directory connection')
  }

  if (connection.mode !== 'remote') {
    return bridge().readDir(path)
  }

  return remoteFsApiForProfile<HermesReadDirResult>(profile, fsPath('list', path))
}

export async function readDesktopFileText(path: string): Promise<HermesReadFileTextResult> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readFileText(path)
  }

  return remoteFsApi<HermesReadFileTextResult>(fsPath('read-text', path))
}

export async function readDesktopFileTextForProfile(
  profile: string,
  generation: number,
  path: string
): Promise<HermesReadFileTextResult> {
  const connection = await connectionForFsContext(profile, generation)

  if (connection.mode !== 'remote') {
    return bridge().readFileText(path)
  }

  return remoteFsApiForProfile<HermesReadFileTextResult>(profile, fsPath('read-text', path))
}

// Save UTF-8 text back to a file. Local writes go through the hardened Electron
// IPC; remote writes hit the dashboard's POST /api/fs/write-text (same path
// hardening, parent-must-exist, size cap) so the editor behaves identically in
// both modes. Stale-on-disk detection is the caller's job (re-read before save).
export async function writeDesktopFileText(path: string, content: string): Promise<{ path: string }> {
  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    if (!desktop.writeTextFile) {
      throw new Error('Saving is not available')
    }

    return desktop.writeTextFile(path, content)
  }

  const result = await remoteFsApi<{ ok?: boolean; path?: string }>('/api/fs/write-text', { content, path })

  return { path: result.path || path }
}

// Profile-bound variant for operations that outlive a live profile swap. It
// resolves the requested profile's connection directly instead of consulting
// the mutable foreground `$connection` atom.
export async function writeDesktopFileTextForProfile(
  profile: string,
  path: string,
  content: string,
  generation?: number
): Promise<{ path: string }> {
  const desktop = bridge()

  const connection =
    generation === undefined ? await desktop.getConnection(profile) : await connectionForFsContext(profile, generation)

  if (connection.mode !== 'remote') {
    if (!desktop.writeTextFile) {
      throw new Error('Saving is not available')
    }

    return desktop.writeTextFile(path, content)
  }

  const result = await remoteFsApiForProfile<{ ok?: boolean; path?: string }>(profile, '/api/fs/write-text', {
    content,
    path
  })

  return { path: result.path || path }
}

export async function readDesktopFileDataUrl(path: string): Promise<string> {
  if (!isDesktopFsRemoteMode()) {
    return bridge().readFileDataUrl(path)
  }

  const result = await remoteFsApi<string | { dataUrl?: string }>(fsPath('read-data-url', path))

  return typeof result === 'string' ? result : result.dataUrl || ''
}

export async function readDesktopFileDataUrlForProfile(
  profile: string,
  generation: number,
  path: string
): Promise<string> {
  const connection = await connectionForFsContext(profile, generation)

  if (connection.mode !== 'remote') {
    return bridge().readFileDataUrl(path)
  }

  const result = await remoteFsApiForProfile<string | { dataUrl?: string }>(profile, fsPath('read-data-url', path))

  return typeof result === 'string' ? result : result.dataUrl || ''
}

export async function desktopGitRoot(path: string): Promise<string | null> {
  const desktop = bridge()

  if (!isDesktopFsRemoteMode()) {
    return desktop.gitRoot ? desktop.gitRoot(path) : null
  }

  return (await remoteFsApi<{ root: string | null }>(fsPath('git-root', path))).root
}

export async function desktopGitRootForProfile(
  profile: string,
  generation: number,
  path: string
): Promise<string | null> {
  const connection = await connectionForFsContext(profile, generation)
  const desktop = bridge()

  if (connection.mode !== 'remote') {
    return desktop.gitRoot ? desktop.gitRoot(path) : null
  }

  return (await remoteFsApiForProfile<{ root: string | null }>(profile, fsPath('git-root', path))).root
}

export async function desktopDefaultCwd(): Promise<{ branch: string; cwd: string } | null> {
  if (!isDesktopFsRemoteMode()) {
    return null
  }

  return remoteFsApi<{ branch: string; cwd: string }>('/api/fs/default-cwd')
}

export async function desktopDefaultCwdForProfile(profile: string): Promise<{ branch: string; cwd: string } | null> {
  const connection = await bridge().getConnection(profile)

  if (connection.mode !== 'remote') {
    return null
  }

  return remoteFsApiForProfile<{ branch: string; cwd: string }>(profile, '/api/fs/default-cwd')
}

// Reveal a path in the OS file manager (Finder / Explorer / Files). Local only.
export async function revealDesktopPath(path: string): Promise<void> {
  await bridge().revealPath?.(path)
}

// Rename a file/folder in place; returns the new absolute path. Local only.
export async function renameDesktopPath(path: string, newName: string): Promise<string> {
  const desktop = bridge()

  if (!desktop.renamePath) {
    throw new Error('Rename is not available')
  }

  const result = await desktop.renamePath(path, newName)

  return result.path
}

// Move a file/folder to the OS trash (recoverable). Local only.
export async function trashDesktopPath(path: string): Promise<void> {
  const desktop = bridge()

  if (!desktop.trashPath) {
    throw new Error('Delete is not available')
  }

  await desktop.trashPath(path)
}

export async function copyTextToClipboard(text: string): Promise<void> {
  await bridge().writeClipboard(text)
}

// Working-tree-vs-HEAD diff for one file. Empty when unchanged / not a repo.
// Remote gateway → backend git (/api/git/file-diff); local → Electron git.
export async function desktopFileDiff(repoRoot: string, filePath: string): Promise<string> {
  if (isDesktopFsRemoteMode()) {
    const result = await remoteFsApi<{ diff: string }>(
      `/api/git/file-diff?path=${encodeURIComponent(repoRoot)}&file=${encodeURIComponent(filePath)}`
    )

    return result.diff || ''
  }

  const git = bridge().git

  return git?.fileDiff ? git.fileDiff(repoRoot, filePath) : ''
}

export async function desktopFileDiffForProfile(
  profile: string,
  generation: number,
  repoRoot: string,
  filePath: string
): Promise<string> {
  const connection = await connectionForFsContext(profile, generation)

  if (connection.mode === 'remote') {
    const result = await remoteFsApiForProfile<{ diff: string }>(
      profile,
      `/api/git/file-diff?path=${encodeURIComponent(repoRoot)}&file=${encodeURIComponent(filePath)}`
    )

    return result.diff || ''
  }

  const git = bridge().git

  return git?.fileDiff ? git.fileDiff(repoRoot, filePath) : ''
}

export async function selectDesktopPaths(options?: HermesSelectPathsOptions): Promise<string[]> {
  const desktop = bridge()
  const context = captureActiveGatewayProfileContext()
  const connection = await desktop.getConnection(context.profile)

  if (!activeGatewayProfileContextIsCurrent(context)) {
    return []
  }

  if (connection.mode !== 'remote' || !options?.directories) {
    const paths = await desktop.selectPaths(options)

    return activeGatewayProfileContextIsCurrent(context) ? paths : []
  }

  return remotePicker
    ? remotePicker.selectPaths({ ...options, multiple: false }, context.profile, context.generation)
    : []
}

export async function selectDesktopPathsForProfile(
  profile: string,
  options?: HermesSelectPathsOptions
): Promise<string[]> {
  const desktop = bridge()
  const context = captureActiveGatewayProfileContext()

  if (normalizeProfileKey(profile) !== context.profile) {
    return []
  }

  const connection = await desktop.getConnection(profile)

  if (!activeGatewayProfileContextIsCurrent(context)) {
    return []
  }

  if (connection.mode !== 'remote' || !options?.directories) {
    const paths = await desktop.selectPaths(options)

    return activeGatewayProfileContextIsCurrent(context) ? paths : []
  }

  return remotePicker ? remotePicker.selectPaths({ ...options, multiple: false }, profile, context.generation) : []
}
