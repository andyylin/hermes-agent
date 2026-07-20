import type {
  HermesGitBaseBranch,
  HermesGitBranch,
  HermesGitWorktree,
  HermesRepoStatus,
  HermesReviewList,
  HermesReviewShipInfo
} from '@/global'
import {
  $activeGatewayProfile,
  activeGatewayProfileContextIsCurrent,
  normalizeProfileKey
} from '@/store/profile'

import { desktopFsProfile, isDesktopFsRemoteMode } from './desktop-fs'

// Remote-aware git facade. Locally the desktop runs git through Electron
// (window.hermesDesktop.git); on a remote gateway that's the wrong filesystem,
// so we mirror the same surface over the dashboard REST API (/api/git/*) — the
// coding rail, worktree lanes, review pane, and branch ops then act on the
// BACKEND repo where sessions actually run. Mirrors desktop-fs.ts.

type GitBridge = NonNullable<NonNullable<Window['hermesDesktop']>['git']>

function desktopApi<T>(path: string, body?: Record<string, unknown>, profile = desktopFsProfile()): Promise<T> {
  const desktop = window.hermesDesktop

  if (!desktop) {
    throw new Error('Hermes Desktop bridge is unavailable')
  }

  return desktop.api<T>(body ? { body, method: 'POST', path, profile } : { path, profile })
}

function gitGet<T>(
  route: string,
  params: Record<string, boolean | null | string | undefined>,
  profile?: string
): Promise<T> {
  const query = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined) {
      query.set(key, String(value))
    }
  }

  return desktopApi<T>(`/api/git/${route}?${query.toString()}`, undefined, profile)
}

function gitPost<T>(route: string, body: Record<string, unknown>, profile?: string): Promise<T> {
  return desktopApi<T>(`/api/git/${route}`, body, profile)
}

function createRemoteGit(profile: () => string | undefined = desktopFsProfile): GitBridge {
  return {
    worktreeList: async repoPath =>
      (await gitGet<{ worktrees: HermesGitWorktree[] }>('worktrees', { path: repoPath }, profile())).worktrees,

    worktreeAdd: (repoPath, options) => gitPost('worktree/add', { path: repoPath, ...options }, profile()),

    worktreeRemove: (repoPath, worktreePath, options) =>
      gitPost('worktree/remove', { force: options?.force ?? false, path: repoPath, worktreePath }, profile()),

    branchSwitch: (repoPath, branch) => gitPost('branch/switch', { branch, path: repoPath }, profile()),

    branchList: async repoPath =>
      (await gitGet<{ branches: HermesGitBranch[] }>('branches', { path: repoPath }, profile())).branches,

    baseBranchList: async repoPath =>
      (await gitGet<{ branches: HermesGitBaseBranch[] }>('base-branches', { path: repoPath }, profile())).branches,

    repoStatus: repoPath => gitGet<HermesRepoStatus | null>('status', { path: repoPath }, profile()),

    fileDiff: async (repoPath, filePath) =>
      (await gitGet<{ diff: string }>('file-diff', { file: filePath, path: repoPath }, profile())).diff,

    review: {
      list: (repoPath, scope, baseRef) =>
        gitGet<HermesReviewList>('review/list', { base: baseRef, path: repoPath, scope }, profile()),

      diff: async (repoPath, filePath, scope, baseRef, staged) =>
        (
          await gitGet<{ diff: string }>(
            'review/diff',
            { base: baseRef, file: filePath, path: repoPath, scope, staged },
            profile()
          )
        ).diff,

      stage: (repoPath, filePath) => gitPost('review/stage', { file: filePath ?? null, path: repoPath }, profile()),

      unstage: (repoPath, filePath) => gitPost('review/unstage', { file: filePath ?? null, path: repoPath }, profile()),

      revert: (repoPath, filePath) => gitPost('review/revert', { file: filePath ?? null, path: repoPath }, profile()),

      revParse: async (repoPath, ref) =>
        (await gitGet<{ sha: null | string }>('review/rev-parse', { path: repoPath, ref }, profile())).sha,

      commit: (repoPath, message, push) => gitPost('review/commit', { message, path: repoPath, push }, profile()),

      commitContext: repoPath => gitGet('review/commit-context', { path: repoPath }, profile()),

      push: repoPath => gitPost('review/push', { path: repoPath }, profile()),

      shipInfo: repoPath => gitGet<HermesReviewShipInfo>('review/ship-info', { path: repoPath }, profile()),

      createPr: repoPath => gitPost('review/create-pr', { path: repoPath }, profile())
    },

    // Repo discovery is a local-disk crawl; on a remote gateway the backend
    // already merges session-derived repos, so this is a no-op.
    scanRepos: async () => []
  }
}

const remoteGit = createRemoteGit()

export function desktopGit(): GitBridge | undefined {
  // Profile activation publishes before the foreground connection atom finishes
  // synchronizing. Refuse that brief mismatch instead of routing B's operation
  // through A's local/remote facade.
  if (normalizeProfileKey(desktopFsProfile()) !== normalizeProfileKey($activeGatewayProfile.get())) {
    return undefined
  }

  return isDesktopFsRemoteMode() ? remoteGit : window.hermesDesktop?.git
}

function guardedGitBridge(git: GitBridge, profile: string, generation: number): GitBridge {
  const context = { generation, profile: normalizeProfileKey(profile) }

  const assertOwned = () => {
    if (!activeGatewayProfileContextIsCurrent(context)) {
      throw new Error('Desktop Git profile ownership changed')
    }
  }

  return new Proxy(git, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver)

      if (typeof value !== 'function') {
        return value
      }

      return (...args: unknown[]) => {
        assertOwned()
        const result = Reflect.apply(value, target, args)
        const promiseLike = result as null | undefined | { then?: unknown }

        if (promiseLike && typeof promiseLike.then === 'function') {
          return Promise.resolve(result).then((resolved: unknown) => {
            assertOwned()

            return resolved
          })
        }

        assertOwned()

        return result
      }
    }
  }) as GitBridge
}

export async function desktopGitForProfile(profile: string, generation: number): Promise<GitBridge | undefined> {
  const desktop = window.hermesDesktop
  const context = { generation, profile: normalizeProfileKey(profile) }

  if (!desktop || !activeGatewayProfileContextIsCurrent(context)) {
    return undefined
  }

  const connection = await desktop.getConnection(profile)

  if (!activeGatewayProfileContextIsCurrent(context)) {
    return undefined
  }

  const git = connection.mode === 'remote' ? createRemoteGit(() => profile) : desktop.git

  return git ? guardedGitBridge(git, profile, generation) : undefined
}

// Repo discovery must bind to the initiating profile. During a live profile
// swap `$activeGatewayProfile` is published before the foreground connection
// atom finishes synchronizing; selecting `desktopGit()` in that gap can crawl
// the old local machine and submit its paths to the new profile.
export async function scanDesktopReposForProfile(
  profile: string,
  generation: number
): Promise<{ label: string; root: string }[]> {
  const desktop = window.hermesDesktop
  const context = { generation, profile: normalizeProfileKey(profile) }

  if (!desktop || !activeGatewayProfileContextIsCurrent(context)) {
    return []
  }

  const connection = await desktop.getConnection(profile)

  if (!activeGatewayProfileContextIsCurrent(context) || connection.mode === 'remote') {
    return []
  }

  const repos = await desktop.git?.scanRepos([])

  return activeGatewayProfileContextIsCurrent(context) ? (repos ?? []) : []
}
