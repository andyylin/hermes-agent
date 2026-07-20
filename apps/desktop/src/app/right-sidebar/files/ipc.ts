import ignore from 'ignore'

import type { HermesReadDirEntry, HermesReadDirResult } from '@/global'
import {
  desktopFsCacheKey,
  desktopGitRoot,
  desktopGitRootForProfile,
  readDesktopDir,
  readDesktopDirForProfile,
  readDesktopFileDataUrl,
  readDesktopFileDataUrlForProfile
} from '@/lib/desktop-fs'
import { ALWAYS_EXCLUDED } from '@/lib/excluded-paths'
import { activeGatewayProfileContextIsCurrent } from '@/store/profile'

export type ProjectTreeEntry = HermesReadDirEntry

interface GitignoreRule {
  base: string
  ig: ReturnType<typeof ignore>
}

export interface ProjectTreeOwner {
  generation: number
  profile: string
}

const gitRootCache = new Map<string, Promise<string | null>>()
const gitignoreCache = new Map<string, Promise<GitignoreRule | null>>()

function decodeDataUrl(dataUrl: string) {
  const match = dataUrl.match(/^data:[^,]*,(.*)$/)
  const data = match?.[1] || ''
  const isBase64 = dataUrl.slice(0, dataUrl.indexOf(',')).includes(';base64')

  if (!isBase64) {
    return decodeURIComponent(data)
  }

  const bytes = Uint8Array.from(atob(data), ch => ch.charCodeAt(0))

  return new TextDecoder().decode(bytes)
}

function clean(path: string) {
  return path.replace(/\\/g, '/').replace(/\/+$/, '') || '/'
}

/** Strict POSIX-style relative path; null if `child` is not inside `root`. */
function relativeTo(root: string, child: string) {
  const r = clean(root)
  const c = clean(child)

  if (c === r) {
    return ''
  }

  return c.startsWith(`${r}/`) ? c.slice(r.length + 1) : null
}

/** Repo-root → repo-root/a → repo-root/a/b → … for every dir between root and `dir`. */
function ancestorDirs(root: string, dir: string) {
  const r = clean(root)
  const rel = relativeTo(r, dir)

  if (rel === null || rel === '') {
    return [r]
  }

  const dirs = [r]
  let current = r

  for (const part of rel.split('/').filter(Boolean)) {
    current = `${current}/${part}`
    dirs.push(current)
  }

  return dirs
}

async function gitRootFor(start: string, owner?: ProjectTreeOwner) {
  const key = `${desktopFsCacheKey()}:${owner?.profile || ''}:${owner?.generation ?? ''}:${clean(start)}`
  let cached = gitRootCache.get(key)

  if (!cached) {
    cached = owner
      ? desktopGitRootForProfile(owner.profile, owner.generation, clean(start))
      : desktopGitRoot(clean(start))
    gitRootCache.set(key, cached)
  }

  return cached
}

/** Read .gitignore at `dir` if it actually exists — never probe missing files. */
async function readGitignore(dir: string, owner?: ProjectTreeOwner): Promise<GitignoreRule | null> {
  try {
    const listing = owner
      ? await readDesktopDirForProfile(owner.profile, owner.generation, dir)
      : await readDesktopDir(dir)

    if (!listing.entries.some(e => e.name === '.gitignore' && !e.isDirectory)) {
      return null
    }

    const dataUrl = owner
      ? await readDesktopFileDataUrlForProfile(owner.profile, owner.generation, `${dir}/.gitignore`)
      : await readDesktopFileDataUrl(`${dir}/.gitignore`)

    const text = decodeDataUrl(dataUrl)

    return { base: dir, ig: ignore().add(text) }
  } catch {
    return null
  }
}

async function gitignoreFor(dir: string, owner?: ProjectTreeOwner) {
  const key = `${desktopFsCacheKey()}:${owner?.profile || ''}:${owner?.generation ?? ''}:${clean(dir)}`
  let cached = gitignoreCache.get(key)

  if (!cached) {
    cached = readGitignore(clean(dir), owner)
    gitignoreCache.set(key, cached)
  }

  return cached
}

function ignoredBy(rules: GitignoreRule[], entry: HermesReadDirEntry) {
  return rules.some(rule => {
    const rel = relativeTo(rule.base, entry.path)

    if (rel === null || rel === '') {
      return false
    }

    return rule.ig.ignores(entry.isDirectory ? `${rel}/` : rel)
  })
}

async function filterIgnored(
  entries: HermesReadDirEntry[],
  rootPath: string,
  dirPath: string,
  owner?: ProjectTreeOwner
) {
  const root = await gitRootFor(rootPath, owner)

  if (!root) {
    return entries
  }

  const rules = (
    await Promise.all(ancestorDirs(root, dirPath).map(dir => gitignoreFor(dir, owner)))
  ).filter((r): r is GitignoreRule => Boolean(r))

  return rules.length > 0 ? entries.filter(entry => !ignoredBy(rules, entry)) : entries
}

export async function readProjectDir(
  dirPath: string,
  rootPath = dirPath,
  owner?: ProjectTreeOwner
): Promise<HermesReadDirResult> {
  if (!window.hermesDesktop) {
    return { entries: [], error: 'no-bridge' }
  }

  try {
    const result = owner
      ? await readDesktopDirForProfile(owner.profile, owner.generation, dirPath)
      : await readDesktopDir(dirPath)

    const entries = (result?.entries ?? []).filter(entry => !ALWAYS_EXCLUDED.has(entry.name))

    return { ...result, entries: await filterIgnored(entries, rootPath, dirPath, owner) }
  } catch (error) {
    if (owner && !activeGatewayProfileContextIsCurrent(owner)) {
      return { entries: [], error: 'stale-profile' }
    }

    throw error
  }
}

export function clearProjectDirCache(rootPath?: string) {
  if (!rootPath) {
    gitRootCache.clear()
    gitignoreCache.clear()

    return
  }

  const key = `${desktopFsCacheKey()}:${clean(rootPath)}`
  gitRootCache.delete(key)
  gitignoreCache.delete(key)
}
