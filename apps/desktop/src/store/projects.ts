import { atom } from 'nanostores'

import { liveSessionProjectId, type SidebarProjectTree } from '@/app/chat/sidebar/projects/workspace-groups'
import type { HermesGitBaseBranch, HermesGitBranch } from '@/global'
import { translateNow } from '@/i18n'
import {
  desktopDefaultCwdForProfile,
  selectDesktopPathsForProfile,
  writeDesktopFileTextForProfile
} from '@/lib/desktop-fs'
import { desktopGitForProfile, scanDesktopReposForProfile } from '@/lib/desktop-git'
import { isMissingRpcMethod } from '@/lib/gateway-rpc'
import { activeGateway, ensureActiveGatewayOpen } from '@/store/gateway'
import { resetProfileBoundWorkspaceLayout, setSidebarAgentsGrouped } from '@/store/layout'
import { notify } from '@/store/notifications'
import {
  $activeGatewayProfile,
  $activeGatewayProfileGeneration,
  activeGatewayProfileContextIsCurrent,
  captureActiveGatewayProfileContext,
  normalizeProfileKey,
  requestFreshSession
} from '@/store/profile'
import { $selectedStoredSessionId, $sessions, sessionMatchesStoredId, workspaceCwdForNewSession } from '@/store/session'
import type { ProjectInfo, ProjectsPayload } from '@/types/hermes'

// First-class, per-profile Projects (named, multi-folder workspaces). State is
// served by the live gateway's `projects.*` JSON-RPC methods, which wrap the
// per-profile projects.db store. The sidebar groups sessions by project folder
// membership; these atoms are the renderer's cached view.

export const $projects = atom<ProjectInfo[]>([])
export const $activeProjectId = atom<null | string>(null)

// The authoritative project -> repo -> lane tree (overview), served by
// `projects.tree`. Lanes carry counts + structure; per-project session rows are
// fetched lazily on drill-in via `fetchProjectSessions`. This is the single
// source of project membership — the desktop no longer derives it.
export const $projectTree = atom<SidebarProjectTree[]>([])
export const $projectTreeLoading = atom(false)

// False when the connected backend predates the projects.* JSON-RPC surface
// (same semver label, older install). Null until the first probe.
export const $projectsRpcAvailable = atom<boolean | null>(null)

function markProjectsRpcSuccess(): void {
  $projectsRpcAvailable.set(true)
}

function markProjectsRpcFailure(err: unknown): void {
  if (isMissingRpcMethod(err)) {
    $projectsRpcAvailable.set(false)
  }
}

function projectsStaleBackendError(): Error {
  return new Error(translateNow('sidebar.projects.staleBackend'))
}

// Client-side cache eviction (Apollo-style optimistic layer): ids the user just
// deleted/archived. The backend tree is a snapshot that still lists them until
// its next refresh, so the render-time overlay strips these so the tree matches
// the live `$sessions` cache exactly — same as the flat Recents list. Pruned on
// refresh once the server snapshot has caught up.
export const $removedSessionIds = atom<Set<string>>(new Set())

export function tombstoneSessions(ids: Array<null | string | undefined>): void {
  const next = new Set($removedSessionIds.get())
  const before = next.size

  for (const id of ids) {
    const trimmed = id?.trim()

    if (trimmed) {
      next.add(trimmed)
    }
  }

  if (next.size !== before) {
    $removedSessionIds.set(next)
  }
}

export function untombstoneSessions(ids: Array<null | string | undefined>): void {
  const current = $removedSessionIds.get()

  if (!current.size) {
    return
  }

  const next = new Set(current)

  for (const id of ids) {
    const trimmed = id?.trim()

    if (trimmed) {
      next.delete(trimmed)
    }
  }

  if (next.size !== current.size) {
    $removedSessionIds.set(next)
  }
}

// True while the disk scan is in flight (drives the "finding repos" hint).
export const $reposScanning = atom(false)

// ── Project scope (the "you're inside a project" view, mirroring profile scope)─
// The sidebar's grouped view is a project switcher: ALL_PROJECTS shows the
// project overview (a list you drill into), and a concrete id means you've
// "entered" that project so only its worktrees/branches/sessions show. This is
// pure view state (localStorage), distinct from the durable active-project
// pointer in projects.db — though entering a project also makes it active so new
// chats land there, exactly as selecting a profile does.
export const ALL_PROJECTS = '__all_projects__'

const PROJECT_SCOPE_KEY = 'hermes.desktop.projectScope'
const projectScopeStorageKey = (profile: string): string => `${PROJECT_SCOPE_KEY}.${encodeURIComponent(profile)}`

function readProjectScope(profile: string): string {
  try {
    return window.localStorage.getItem(projectScopeStorageKey(profile)) || ALL_PROJECTS
  } catch {
    return ALL_PROJECTS
  }
}

function writeProjectScope(profile: string, value: string): void {
  try {
    window.localStorage.setItem(projectScopeStorageKey(profile), value || ALL_PROJECTS)
  } catch {
    // Storage can be unavailable in hardened/private renderer contexts. The
    // in-memory scope still works for this window.
  }
}

let projectProfileKey = normalizeProfileKey($activeGatewayProfile.get())
let projectProfileGeneration = 0
let loadingStoredProjectScope = false

export const $projectScope = atom<string>(readProjectScope(projectProfileKey))

$projectScope.subscribe(value => {
  if (!loadingStoredProjectScope) {
    writeProjectScope(projectProfileKey, value)
  }
})

// Enter a project: scope the sidebar to it and make it the active project
// (best-effort — the durable pointer is nice-to-have, the view scope is the
// point). Never opens a session.
export function enterProject(id: string): void {
  $projectScope.set(id)

  // Only explicit, persisted projects (ids are `p_<hex>`) become active. Auto
  // projects (ids are filesystem paths) and the "No project" bucket have no
  // durable row to pin, so they're view-scope only.
  if (id.startsWith('p_')) {
    void setActiveProject(id).catch(() => undefined)
  }
}

export function exitProjectScope(): void {
  $projectScope.set(ALL_PROJECTS)
}

// The cwd a NEW chat should start in. The "active project" is just an atom
// ($projectScope) — so when you're inside a project, a new session (cmd-n, the
// trunk "+") starts at that project's root (its primary repo = the default-branch
// checkout) instead of inheriting whatever unrelated worktree the live cwd
// drifted into. Outside a project it falls back to the plain default (detached),
// so a bare new chat shows no branch.
export function resolveNewSessionCwd(): string {
  const scope = $projectScope.get()

  if (scope !== ALL_PROJECTS) {
    const project = $projectTree.get().find(node => node.id === scope)
    const cwd = (project?.path || project?.repos.find(repo => repo.path)?.path || '').trim()

    if (cwd) {
      return cwd
    }
  }

  return workspaceCwdForNewSession()
}

const underPath = (parent: string, child: string): boolean =>
  child === parent || child.startsWith(parent.endsWith('/') ? parent : `${parent}/`)

// The project (explicit or auto) that owns `cwd`, by longest path match across
// the live tree. Null when no project covers it (it'll surface as a fresh
// auto-project on the next tree refresh).
export function projectIdForCwd(cwd: string): null | string {
  let best: null | string = null
  let bestLen = -1

  for (const project of $projectTree.get()) {
    // Match project + repo roots AND each worktree-lane path: a linked worktree
    // (e.g. a sibling `repo-retry`) lives OUTSIDE the repo root, so root-prefix
    // matching alone would miss it — but it's still part of the project.
    const paths = [project.path, ...project.repos.flatMap(repo => [repo.path, ...repo.groups.map(group => group.path)])]

    for (const path of paths) {
      const p = (path || '').trim()

      if (p && underPath(p, cwd) && p.length > bestLen) {
        bestLen = p.length
        best = project.id
      }
    }
  }

  return best
}

// The display NAME of the explicit, named project owning `cwd` (longest path
// match), or null when the cwd sits in no named project. The status bar reads
// this to label the workspace by project instead of the bare cwd leaf. We skip
// auto-projects (a repo root promoted with no projects.db row) and the synthetic
// "No project" bucket on purpose: those have no human name, so their sessions
// keep the cwd-leaf label — matching the backend `_project_info_for_cwd`, which
// only resolves projects.db rows, so the desktop and TUI name the same session
// identically without threading a second per-session copy through session.info.
export function projectNameForCwd(cwd: string): null | string {
  const target = (cwd || '').trim()

  if (!target) {
    return null
  }

  let best: null | string = null
  let bestLen = -1

  for (const project of $projectTree.get()) {
    if (project.isAuto || project.isNoProject) {
      continue
    }

    const paths = [project.path, ...project.repos.flatMap(repo => [repo.path, ...repo.groups.map(group => group.path)])]

    for (const path of paths) {
      const p = (path || '').trim()

      if (p && underPath(p, target) && p.length > bestLen) {
        bestLen = p.length
        best = project.label
      }
    }
  }

  return best
}

// The active session's agent relocated itself (created/entered another repo or
// worktree via the terminal — backend re-anchors its cwd and emits session.info).
// Re-pull projects + tree so a freshly created/auto project and the relocated
// session row show live, then follow the view into the session's new project
// (from the overview or a now-stale project alike). Caller gates this on a real
// same-session cwd move, so a plain session switch never reaches here.
export async function followActiveSessionCwd(cwd: string): Promise<void> {
  const target = cwd.trim()

  if (!target) {
    return
  }

  let context: ProjectRequestContext

  try {
    context = await captureProjectRequestContext()
    assertProjectContextCurrent(context)
    await Promise.all([refreshProjects(), refreshProjectTree()])
    assertProjectContextCurrent(context)
  } catch {
    return
  }

  // Resolve only after the refresh, so a just-created/auto project is in the tree.
  const projectId = projectIdForCwd(target)

  if (projectId) {
    // The Projects tree only renders in grouped mode, so flip the sidebar into
    // it — otherwise following from the flat Sessions list would change scope
    // invisibly. Then drill into the thread's project.
    setSidebarAgentsGrouped(true)

    if (projectId !== $projectScope.get()) {
      enterProject(projectId)
    }
  }
}

// Project RPCs are profile-bound. Capture both the profile generation and the
// concrete socket before the first await; a live A→B→A swap must not let an old
// A response publish into the new A view, and a delayed write must never be
// retargeted through whichever gateway happens to be active later.
type ProjectGateway = NonNullable<ReturnType<typeof activeGateway>>

interface ProjectRequestContext {
  gateway: ProjectGateway
  generation: number
  profile: string
}

class StaleProjectProfileError extends Error {
  constructor() {
    super('Project operation belongs to a stale profile context')
    this.name = 'StaleProjectProfileError'
  }
}

function projectContextIsCurrent(context: ProjectRequestContext): boolean {
  return (
    context.generation === projectProfileGeneration &&
    context.profile === normalizeProfileKey($activeGatewayProfile.get()) &&
    context.gateway === activeGateway()
  )
}

function assertProjectContextCurrent(context: ProjectRequestContext): void {
  if (!projectContextIsCurrent(context)) {
    throw new StaleProjectProfileError()
  }
}

async function captureProjectRequestContext(): Promise<ProjectRequestContext> {
  const profile = normalizeProfileKey($activeGatewayProfile.get())
  const generation = projectProfileGeneration
  let gateway = activeGateway()

  if (!gateway || gateway.connectionState !== 'open') {
    gateway = await ensureActiveGatewayOpen()
  }

  if (!gateway) {
    throw new Error('Hermes gateway is not connected')
  }

  const context = { gateway, generation, profile }

  if (!projectContextIsCurrent(context)) {
    throw new StaleProjectProfileError()
  }

  return context
}

async function gatewayRequest<T>(
  method: string,
  params: Record<string, unknown> = {},
  context?: ProjectRequestContext
): Promise<T> {
  const captured = context ?? (await captureProjectRequestContext())
  assertProjectContextCurrent(captured)

  try {
    const result = await captured.gateway.request<T>(method, params)

    if (!projectContextIsCurrent(captured)) {
      throw new StaleProjectProfileError()
    }

    return result
  } catch (err) {
    // A network/backend error that finishes after a profile swap is stale too;
    // callers must not roll the old profile's optimistic snapshot into the new
    // view just because the original request rejected.
    if (!projectContextIsCurrent(captured)) {
      throw new StaleProjectProfileError()
    }

    throw err
  }
}

function applyPayload(payload: ProjectsPayload): void {
  $projects.set(payload.projects ?? [])
  $activeProjectId.set(payload.active_id ?? null)
}

// Pull the full project list + active pointer. Best-effort: a failure (gateway
// not up yet) leaves the cached atoms intact so the sidebar doesn't flicker.
export async function refreshProjects(): Promise<void> {
  let context: ProjectRequestContext | null = null

  try {
    context = await captureProjectRequestContext()
    const payload = await gatewayRequest<ProjectsPayload>('projects.list', {}, context)

    assertProjectContextCurrent(context)
    applyPayload(payload)
    markProjectsRpcSuccess()
  } catch (err) {
    if (err instanceof StaleProjectProfileError) {
      return
    }

    markProjectsRpcFailure(err)
    // Backend may not be ready; keep the last known list.
  }
}

interface ProjectTreePayload {
  projects: SidebarProjectTree[]
  active_id: null | string
  scoped_session_ids: string[]
}

// Pull the authoritative project tree (overview structure + counts + preview
// sessions + the scoped-session-id set). Best-effort: a failure leaves the
// cached tree intact so the sidebar doesn't flicker.
export async function refreshProjectTree(): Promise<void> {
  let context: ProjectRequestContext | null = null

  try {
    context = await captureProjectRequestContext()
    assertProjectContextCurrent(context)
    $projectTreeLoading.set(true)
    const res = await gatewayRequest<ProjectTreePayload>('projects.tree', { preview_limit: 3 }, context)
    assertProjectContextCurrent(context)
    // The flat Sessions list shows everything; scoped ids are only used here to
    // reconcile the optimistic eviction layer against what the server still lists.
    const scoped = new Set(res.scoped_session_ids ?? [])

    $projectTree.set(res.projects ?? [])
    $activeProjectId.set(res.active_id ?? null)

    // Reconcile the optimistic eviction layer against the fresh snapshot: keep
    // evicting ids the server still lists (delete in flight) and drop the rest
    // (server caught up), so the set can't grow unbounded across a long session.
    const tombstones = $removedSessionIds.get()

    if (tombstones.size) {
      const pending = new Set([...tombstones].filter(id => scoped.has(id)))

      if (pending.size !== tombstones.size) {
        $removedSessionIds.set(pending)
      }
    }

    markProjectsRpcSuccess()
  } catch (err) {
    markProjectsRpcFailure(err)
    // Backend may not be ready; keep the last known tree.
  } finally {
    if (context && projectContextIsCurrent(context)) {
      $projectTreeLoading.set(false)
    }
  }
}

// Fully hydrated lanes (repo -> lane -> session rows) for one project, fetched
// when the user enters it. Same backend grouping as `projects.tree`, so ids and
// membership match exactly.
export async function fetchProjectSessions(projectId: string): Promise<SidebarProjectTree | null> {
  try {
    const context = await captureProjectRequestContext()

    const res = await gatewayRequest<{ project: SidebarProjectTree | null }>(
      'projects.project_sessions',
      { project_id: projectId },
      context
    )

    assertProjectContextCurrent(context)

    return res.project ?? null
  } catch {
    return null
  }
}

// One filesystem scan per profile per app run: the heavy disk walk happens once
// for each profile backend. A profile switch while the crawler is in flight
// invalidates its context, so the result is never submitted through another
// profile's gateway and the original profile remains eligible for a retry.
const scannedRepoProfiles = new Set<string>()

export async function scanAndRecordRepos(force = false): Promise<void> {
  let context: ProjectRequestContext

  try {
    context = await captureProjectRequestContext()
    assertProjectContextCurrent(context)
  } catch {
    return
  }

  if (scannedRepoProfiles.has(context.profile) && !force) {
    return
  }

  scannedRepoProfiles.add(context.profile)

  if (projectContextIsCurrent(context)) {
    $reposScanning.set(true)
  }

  try {
    const repos = await scanDesktopReposForProfile(context.profile)
    await gatewayRequest('projects.record_repos', { repos }, context)
    assertProjectContextCurrent(context)
    // The disk scan may surface new zero-session repos; refold them into the tree.
    await refreshProjectTree()
  } catch {
    scannedRepoProfiles.delete(context.profile) // let that profile retry a failed/stale scan
  } finally {
    if (projectContextIsCurrent(context)) {
      $reposScanning.set(false)
    }
  }
}

export interface CreateProjectInput {
  name: string
  folders?: string[]
  primaryPath?: string
  slug?: string
  description?: string
  icon?: string
  color?: string
  boardSlug?: string
  use?: boolean
  // Free-text project idea; written to IDEA.md at the primary folder on create.
  idea?: string
}

// Generate a project idea via the stateless llm.oneshot RPC (inherits the live
// session's model when one exists). Returns "" on failure so the caller can just
// leave the field untouched. The "🎲" affordance in the new-project dialog.
export async function generateProjectIdea(name: string): Promise<string> {
  try {
    const context = await captureProjectRequestContext()

    const res = await gatewayRequest<{ text: string }>(
      'llm.oneshot',
      {
        instructions:
          'You generate a single, concrete project idea as a short IDEA.md body: a one-line summary, ' +
          'then 3-5 bullet goals. No preamble, no code fences, under 120 words.',
        input: name.trim() ? `Project name: ${name.trim()}` : 'Surprise me with a fun project.',
        temperature: 1.0
      },
      context
    )

    assertProjectContextCurrent(context)

    return (res.text || '').trim()
  } catch {
    return ''
  }
}

// Write IDEA.md to a project's primary folder (best-effort). Routes through the
// remote-aware fs write, so it lands on the backend for a remote gateway and on
// disk locally — the project is created regardless of whether the file lands.
async function writeProjectIdea(profile: string, folder: null | string | undefined, idea: string): Promise<void> {
  const dir = (folder || '').trim()
  const body = idea.trim()

  if (!dir || !body) {
    return
  }

  try {
    await writeDesktopFileTextForProfile(
      profile,
      `${dir.replace(/[/\\]+$/, '')}/IDEA.md`,
      body.endsWith('\n') ? body : `${body}\n`
    )
  } catch {
    // Best-effort: the project is created regardless of whether IDEA.md lands.
  }
}

// ── Optimistic cache layer ───────────────────────────────────────────────────
// The project cache (list + tree + active pointer) mutates instantly on user
// action; the write reconciles in the background and rolls the whole cache back
// on failure — the same Apollo-style layer the session list uses.

interface ProjectsSnapshot {
  projects: ProjectInfo[]
  tree: SidebarProjectTree[]
  active: null | string
}

const snapshotProjects = (): ProjectsSnapshot => ({
  projects: $projects.get(),
  tree: $projectTree.get(),
  active: $activeProjectId.get()
})

const restoreProjects = ({ projects, tree, active }: ProjectsSnapshot): void => {
  $projects.set(projects)
  $projectTree.set(tree)
  $activeProjectId.set(active)
}

// Await an already-applied optimistic write; restore the snapshot if it throws.
async function persistOrRollback(snap: ProjectsSnapshot, write: () => Promise<void>): Promise<void> {
  try {
    await write()
  } catch (err) {
    if (!(err instanceof StaleProjectProfileError)) {
      restoreProjects(snap)
    }

    throw err
  }
}

const reconcileProjects = (): void => {
  void refreshProjects()
  void refreshProjectTree()
}

// Map a ProjectInfo (list shape) onto a minimal overview tree node so a created
// project paints instantly. The backend seeds each folder as an (empty) repo, so
// the next tree refresh fills in repos/counts; this is just the optimistic stub.
function projectInfoToTreeNode(project: ProjectInfo): SidebarProjectTree {
  return {
    id: project.id,
    label: project.name || project.id,
    path: project.primary_path ?? project.folders?.[0]?.path ?? null,
    color: project.color ?? null,
    icon: project.icon ?? null,
    isAuto: false,
    repos: [],
    sessionCount: 0,
    previewSessions: []
  }
}

export async function createProject(input: CreateProjectInput): Promise<ProjectInfo | null> {
  if ($projectsRpcAvailable.get() === false) {
    throw projectsStaleBackendError()
  }

  let context: ProjectRequestContext
  let res: { project: ProjectInfo | null }

  try {
    context = await captureProjectRequestContext()
    assertProjectContextCurrent(context)
    res = await gatewayRequest<{ project: ProjectInfo | null }>(
      'projects.create',
      {
        name: input.name,
        folders: input.folders ?? [],
        primary_path: input.primaryPath,
        slug: input.slug,
        description: input.description,
        icon: input.icon,
        color: input.color,
        board_slug: input.boardSlug,
        use: input.use ?? false
      },
      context
    )
  } catch (err) {
    if (err instanceof StaleProjectProfileError) {
      return null
    }

    if (isMissingRpcMethod(err)) {
      $projectsRpcAvailable.set(false)
      throw projectsStaleBackendError()
    }

    throw err
  }

  if (!projectContextIsCurrent(context)) {
    return null
  }

  markProjectsRpcSuccess()

  // Not optimistic (the create awaits the RPC first, so there's nothing to roll
  // back): apply the server's row into the cached list + tree at once, so it
  // (and an entered scope) shows without waiting on the background refreshes
  // that reconcile counts/repos.
  const created = res.project

  if (created) {
    if (input.idea) {
      void writeProjectIdea(
        context.profile,
        created.primary_path ?? created.folders?.[0]?.path ?? input.primaryPath,
        input.idea
      )
    }

    if (!$projects.get().some(proj => proj.id === created.id)) {
      $projects.set([...$projects.get(), created])
    }

    if (!$projectTree.get().some(node => node.id === created.id)) {
      $projectTree.set([projectInfoToTreeNode(created), ...$projectTree.get()])
    }

    if (input.use) {
      $activeProjectId.set(created.id)
    }

    setSidebarAgentsGrouped(true)
  }

  reconcileProjects()

  return created
}

export async function renameProject(id: string, name: string): Promise<void> {
  await updateProject(id, { name })
}

// Patch top-level project fields (name / appearance). Optimistic: the cached
// tree + list update instantly so a color/icon/name change has no round-trip
// lag; only a failed write reconciles from the server.
export async function updateProject(
  id: string,
  patch: { name?: string; color?: null | string; icon?: null | string }
): Promise<void> {
  const context = await captureProjectRequestContext()
  assertProjectContextCurrent(context)
  const snap = snapshotProjects()

  $projectTree.set(
    snap.tree.map(node =>
      node.id === id
        ? {
            ...node,
            ...(patch.name !== undefined && { label: patch.name }),
            ...(patch.color !== undefined && { color: patch.color }),
            ...(patch.icon !== undefined && { icon: patch.icon })
          }
        : node
    )
  )
  $projects.set(snap.projects.map(proj => (proj.id === id ? { ...proj, ...patch } : proj)))

  // Backend treats null/undefined as "leave unchanged"; "" clears (stores NULL).
  // Map explicit null → "" so "no color"/"no icon" actually clear.
  await persistOrRollback(snap, () =>
    gatewayRequest(
      'projects.update',
      {
        id,
        ...patch,
        ...(patch.color === null && { color: '' }),
        ...(patch.icon === null && { icon: '' })
      },
      context
    )
  )
}

// Appearance for an AUTO (inherited git-repo) project has no projects.db row to
// write to — its id is just the repo path. So the first color/icon change ADOPTS
// the repo as a real project (folder = repo root, name = its label) carrying the
// chosen look; from then on it patches in place like any explicit project.
// Returns true when an adoption happened, so an incremental picker can close
// (the node's id changes on adopt, and a second stale write would double-create).
export async function setProjectAppearance(
  project: Pick<SidebarProjectTree, 'color' | 'icon' | 'id' | 'isAuto' | 'label' | 'path'>,
  patch: { color?: null | string; icon?: null | string }
): Promise<boolean> {
  if (!project.isAuto) {
    await updateProject(project.id, patch)

    return false
  }

  if (!project.path) {
    return false
  }

  await createProject({
    name: project.label,
    folders: [project.path],
    primaryPath: project.path,
    // Carry any already-set look so setting one field doesn't wipe the other.
    color: (patch.color ?? project.color) || undefined,
    icon: (patch.icon ?? project.icon) || undefined
  })

  return true
}

export async function addProjectFolder(
  id: string,
  path: string,
  opts: { label?: string; isPrimary?: boolean } = {}
): Promise<void> {
  const context = await captureProjectRequestContext()
  assertProjectContextCurrent(context)
  const snap = snapshotProjects()
  const trimmed = path.trim()

  // Optimistic: append the folder to the cached project + reflect a primary-path
  // change on its tree node, so the dialog closes onto an updated row. The folder
  // -> repo seeding (and session regrouping) is backend-computed, so the
  // background refresh fills repos in; a failure rolls the cache back.
  if (trimmed) {
    const folder = { path: trimmed, label: opts.label ?? null, is_primary: opts.isPrimary ?? false, added_at: 0 }

    $projects.set(
      snap.projects.map(proj => {
        if (proj.id !== id || proj.folders?.some(f => f.path === trimmed)) {
          return proj
        }

        const folders = opts.isPrimary
          ? [folder, ...proj.folders.map(f => ({ ...f, is_primary: false }))]
          : [...proj.folders, folder]

        return { ...proj, folders, ...(opts.isPrimary && { primary_path: trimmed }) }
      })
    )

    if (opts.isPrimary) {
      $projectTree.set(snap.tree.map(node => (node.id === id ? { ...node, path: trimmed } : node)))
    }
  }

  await persistOrRollback(snap, () =>
    gatewayRequest('projects.add_folder', { id, path, label: opts.label, is_primary: opts.isPrimary ?? false }, context)
  )
  reconcileProjects()
}

// True when the session currently open in the main pane belongs to `projectId`.
// Used so deleting a project you have a session open from kicks you back to the
// intro draft instead of stranding you in a now-orphaned view.
function openSessionBelongsToProject(projectId: string, projects: ProjectInfo[]): boolean {
  const openId = $selectedStoredSessionId.get()

  if (!openId) {
    return false
  }

  const open = $sessions.get().find(s => sessionMatchesStoredId(s, openId))

  return Boolean(open && liveSessionProjectId(open, projects) === projectId)
}

// Optimistic: drop the project from the cached tree + list the instant it's
// clicked (the entered-scope effect exits if you deleted the project you were
// inside), reconciling from the server payload. A failed delete restores both.
export async function deleteProject(id: string): Promise<void> {
  const context = await captureProjectRequestContext()
  assertProjectContextCurrent(context)
  const snap = snapshotProjects()
  // Capture membership BEFORE removal — the project's folders (which determine
  // ownership) are gone once it's dropped from the cache.
  const kickToIntro = openSessionBelongsToProject(id, snap.projects)

  $projects.set(snap.projects.filter(project => project.id !== id))
  $projectTree.set(snap.tree.filter(node => node.id !== id))

  if (snap.active === id) {
    $activeProjectId.set(null)
  }

  // The open session's project is gone — reset to the intro draft (the session
  // itself survives; it just falls back to Recents).
  if (kickToIntro) {
    requestFreshSession()
  }

  await persistOrRollback(snap, async () => {
    const payload = await gatewayRequest<ProjectsPayload>('projects.delete', { id }, context)

    assertProjectContextCurrent(context)
    applyPayload(payload)
  })
  void refreshProjectTree()
}

export async function setActiveProject(id: null | string): Promise<void> {
  const context = await captureProjectRequestContext()
  const res = await gatewayRequest<{ active_id: null | string }>('projects.set_active', { id }, context)

  assertProjectContextCurrent(context)
  $activeProjectId.set(res.active_id ?? null)
}

// ── Project management dialog ────────────────────────────────────────────────
// A single dialog mounted in the sidebar reads this atom, so a project node's
// menu can open create / rename / add-folder flows without prop threading
// (mirrors $profileCreateRequest).
export interface ProjectDialogState {
  mode: 'add-folder' | 'create' | 'rename'
  projectId?: string
  name?: string
}

export const $projectDialog = atom<null | ProjectDialogState>(null)

export function openProjectCreate(): void {
  if ($projectsRpcAvailable.get() === false) {
    notify({
      kind: 'warning',
      message: translateNow('sidebar.projects.staleBackend')
    })

    return
  }

  $projectDialog.set({ mode: 'create' })
}

export function openProjectRename(project: { id: string; name: string }): void {
  $projectDialog.set({ mode: 'rename', name: project.name, projectId: project.id })
}

export function openProjectAddFolder(project: { id: string; name: string }): void {
  $projectDialog.set({ mode: 'add-folder', name: project.name, projectId: project.id })
}

export function closeProjectDialog(): void {
  $projectDialog.set(null)
}

// A live profile swap is a re-home, not a repaint over the previous backend's
// cache. Wipe every profile-bound atom synchronously, restore only that profile's
// persisted view scope, and bump the generation so all in-flight A operations
// remain stale even if the user rapidly switches A→B→A.
$activeGatewayProfile.subscribe(value => {
  const nextProfile = normalizeProfileKey(value)

  if (nextProfile === projectProfileKey) {
    return
  }

  projectProfileKey = nextProfile
  projectProfileGeneration += 1
  resetProfileBoundWorkspaceLayout()
  $projects.set([])
  $activeProjectId.set(null)
  $projectTree.set([])
  $projectTreeLoading.set(false)
  $projectsRpcAvailable.set(null)
  $removedSessionIds.set(new Set())
  $reposScanning.set(false)
  $projectDialog.set(null)

  loadingStoredProjectScope = true
  $projectScope.set(readProjectScope(nextProfile))
  loadingStoredProjectScope = false
})

// ── Git-driven worktrees ("Start work") ─────────────────────────────────────
// Bumped after a `git worktree add`/`remove` so the sidebar's worktree-list
// probe (useRepoWorktreeMap) refetches and the new/removed lane shows at once,
// instead of waiting for the next scope change.
export const $worktreeRefreshToken = atom(0)
const bumpWorktrees = () => $worktreeRefreshToken.set($worktreeRefreshToken.get() + 1)

async function captureProjectGitContext() {
  const context = await captureProjectRequestContext()
  assertProjectContextCurrent(context)
  const git = await desktopGitForProfile(context.profile)
  assertProjectContextCurrent(context)

  return { context, git }
}

// Re-run the visual `git worktree list` probe without the heavy projects.tree
// scan. Desktop-initiated add/remove already bumps the token inline; this is for
// OUT-OF-BAND changes the renderer can't see: the agent runs `git worktree
// add/remove` in the terminal during a turn, or an external terminal mutates the
// repo while the window was away. The probe is per-repo and bounded, so the
// caller (a settled turn / window refocus) can re-sync the worktree lanes
// cheaply, the same way a git GUI refreshes its tree on focus.
export function refreshWorktrees(): void {
  bumpWorktrees()
}

// Spin up a fresh worktree the lightest way (`git worktree add -b`) under the
// repo, returning where Hermes should start working. Git is the source of
// truth; the caller starts a session in the returned path.
export async function startWorkInRepo(
  repoPath: string,
  options?: {
    name?: string
    branch?: string
    base?: string
    existingBranch?: string
    profile?: string
    generation?: number
  }
): Promise<null | { path: string; branch: string; profile: string; generation: number }> {
  if (!repoPath) {
    return null
  }

  try {
    const current = captureActiveGatewayProfileContext()

    const handoff = {
      generation: options?.generation ?? current.generation,
      profile: options?.profile ?? current.profile
    }

    if (!activeGatewayProfileContextIsCurrent(handoff)) {
      return null
    }

    const { context, git } = await captureProjectGitContext()

    if (!git || !activeGatewayProfileContextIsCurrent(handoff)) {
      return null
    }

    const worktreeOptions = options
      ? {
          base: options.base,
          branch: options.branch,
          existingBranch: options.existingBranch,
          name: options.name
        }
      : undefined

    const result = await git.worktreeAdd(repoPath, worktreeOptions)
    assertProjectContextCurrent(context)

    if (!activeGatewayProfileContextIsCurrent(handoff)) {
      return null
    }

    bumpWorktrees()

    return { branch: result.branch, generation: handoff.generation, path: result.path, profile: handoff.profile }
  } catch (err) {
    if (err instanceof StaleProjectProfileError) {
      return null
    }

    throw err
  }
}

// Local branches for the composer's "convert a branch into a worktree" picker.
// Empty on a remote backend / non-repo (the Electron probe can't run).
export async function listRepoBranches(
  repoPath: string,
  owner?: { generation: number; profile: string }
): Promise<HermesGitBranch[]> {
  if (!repoPath) {
    return []
  }

  const { context, git } = await captureProjectGitContext()

  if (!git?.branchList || (owner && !activeGatewayProfileContextIsCurrent(owner))) {
    return []
  }

  const branches = await git.branchList(repoPath)
  assertProjectContextCurrent(context)

  return branches
}

// Local + remote-tracking branches for the base-branch picker in the
// new-worktree dialog. The remote default (origin/HEAD) is flagged so the
// UI can preselect it. Empty on a remote backend / non-repo.
export async function listBaseBranches(
  repoPath: string,
  owner?: { generation: number; profile: string }
): Promise<HermesGitBaseBranch[]> {
  if (!repoPath) {
    return []
  }

  const { context, git } = await captureProjectGitContext()

  if (!git?.baseBranchList || (owner && !activeGatewayProfileContextIsCurrent(owner))) {
    return []
  }

  const branches = await git.baseBranchList(repoPath)
  assertProjectContextCurrent(context)

  return branches
}

export async function switchBranchInRepo(
  repoPath: string,
  branch: string,
  owner?: { generation: number; profile: string }
): Promise<null | { generation: number; profile: string }> {
  if (!repoPath || !branch.trim()) {
    return null
  }

  try {
    const handoff = owner ?? captureActiveGatewayProfileContext()

    if (!activeGatewayProfileContextIsCurrent(handoff)) {
      return null
    }

    const { context, git } = await captureProjectGitContext()

    if (!git || !activeGatewayProfileContextIsCurrent(handoff)) {
      return null
    }

    await git.branchSwitch(repoPath, branch)
    assertProjectContextCurrent(context)

    if (!activeGatewayProfileContextIsCurrent(handoff)) {
      return null
    }

    bumpWorktrees()

    return handoff
  } catch (err) {
    if (!(err instanceof StaleProjectProfileError)) {
      throw err
    }

    return null
  }
}

// A composer-driven "branch off into a new worktree" hand-off. The composer
// owns the typed draft; the chat controller owns session lifecycle. The composer
// creates the worktree (startWorkInRepo), then fires this so the controller opens
// a fresh session in that worktree and prefills the draft that kicked off the
// task. A monotonic token lets a rapid second request re-fire the controller's
// effect even if the path repeats.
export interface StartWorkSessionRequest {
  draft?: string
  generation: number
  path: string
  profile: string
  token: number
}

export const $startWorkSessionRequest = atom<StartWorkSessionRequest | null>(null)
export const $startWorkSessionCommitted = atom<StartWorkSessionRequest | null>(null)

$activeGatewayProfileGeneration.subscribe(() => $startWorkSessionRequest.set(null))

// Keyboard-driven "spin up a new worktree" intent. The composer's coding rail
// owns the name dialog (it has the active repo + branch context), so a global
// hotkey just bumps this token; the rail opens its branch-off dialog in
// response. A monotonic token re-fires even on repeat presses. No-ops off a
// repo (the rail isn't mounted), which is the right "nothing to branch" outcome.
export const $newWorktreeRequest = atom(0)

export function requestNewWorktree(): void {
  $newWorktreeRequest.set($newWorktreeRequest.get() + 1)
}

let startWorkToken = 0

export function requestStartWorkSession(
  path: string,
  draft?: string,
  profile?: string,
  generation?: number
): void {
  const target = path.trim()
  const current = captureActiveGatewayProfileContext()
  const owner = normalizeProfileKey(profile ?? current.profile)
  const ownerGeneration = generation ?? current.generation

  if (!target || owner !== current.profile || ownerGeneration !== current.generation) {
    return
  }

  startWorkToken += 1
  $startWorkSessionRequest.set({
    draft: draft?.trim() || undefined,
    generation: ownerGeneration,
    path: target,
    profile: owner,
    token: startWorkToken
  })
}

export function markStartWorkSessionCommitted(token: number): void {
  const request = $startWorkSessionRequest.get()

  if (request?.token === token && activeGatewayProfileContextIsCurrent(request)) {
    $startWorkSessionCommitted.set(request)
  }
}

export async function removeWorktreePath(
  repoPath: string,
  worktreePath: string,
  options?: { force?: boolean; generation?: number; profile?: string }
): Promise<boolean> {
  try {
    const current = captureActiveGatewayProfileContext()

    const owner = {
      generation: options?.generation ?? current.generation,
      profile: options?.profile ?? current.profile
    }

    if (!activeGatewayProfileContextIsCurrent(owner)) {
      return false
    }

    const { context, git } = await captureProjectGitContext()

    if (!git || !activeGatewayProfileContextIsCurrent(owner)) {
      return false
    }

    await git.worktreeRemove(repoPath, worktreePath, { force: options?.force })
    assertProjectContextCurrent(context)

    if (!activeGatewayProfileContextIsCurrent(owner)) {
      return false
    }

    bumpWorktrees()

    return true
  } catch (err) {
    if (!(err instanceof StaleProjectProfileError)) {
      throw err
    }

    return false
  }
}

// Reveal a project/worktree path in the OS file manager (git-GUI standard).
export async function revealPath(path: null | string): Promise<void> {
  if (path) {
    await window.hermesDesktop?.revealPath?.(path)
  }
}

// Copy a path to the clipboard (git-GUI standard).
export async function copyPath(path: null | string): Promise<void> {
  if (path) {
    await window.hermesDesktop?.writeClipboard?.(path)
  }
}

// Pick a project folder via the remote-aware picker: a remote gateway browses
// the backend filesystem (seeded at its default cwd) where sessions run; local
// mode opens the native dialog. Returns the absolute path, or null if cancelled.
export async function pickProjectFolder(): Promise<null | string> {
  try {
    const context = await captureProjectRequestContext()
    const defaultCwd = await desktopDefaultCwdForProfile(context.profile)

    assertProjectContextCurrent(context)

    const [dir] = await selectDesktopPathsForProfile(context.profile, {
      defaultPath: defaultCwd?.cwd,
      directories: true,
      multiple: false
    })

    assertProjectContextCurrent(context)

    return dir || null
  } catch (err) {
    if (err instanceof StaleProjectProfileError) {
      return null
    }

    throw err
  }
}
