import { useStore } from '@nanostores/react'
import { type MutableRefObject, useCallback, useEffect, useRef } from 'react'

import { activeGatewayProfileContextIsCurrent, captureActiveGatewayProfileContext } from '@/store/profile'
import {
  $startWorkSessionCommitted,
  $startWorkSessionRequest,
  listRepoBranches,
  requestStartWorkSession,
  startWorkInRepo,
  switchBranchInRepo
} from '@/store/projects'

import { useComposerScope } from '../scope'

interface UseComposerBranchOptions {
  clearDraft: () => void
  cwd: null | string | undefined
  draftRef: MutableRefObject<string>
}

/**
 * Branch / worktree engine — the `CodingStatusRow` hand-offs. Each action opens
 * a fresh session anchored in a worktree carrying the current composer draft as
 * its first turn; clearing here means the draft travels to the new session
 * instead of getting stashed under this one. Backend coupling (cwd + the
 * projects store) is the only dependency; nothing about ChatBar's render.
 */
export function useComposerBranch({ clearDraft, cwd, draftRef }: UseComposerBranchOptions) {
  const scope = useComposerScope()
  const owner = captureActiveGatewayProfileContext()
  const committed = useStore($startWorkSessionCommitted)
  const pendingClearRef = useRef<null | { generation: number; profile: string; text: string; token: number }>(null)

  useEffect(() => {
    const pending = pendingClearRef.current

    if (
      !pending ||
      pending.token !== committed?.token ||
      pending.profile !== committed.profile ||
      pending.generation !== committed.generation
    ) {
      return
    }

    pendingClearRef.current = null

    if (!activeGatewayProfileContextIsCurrent(pending)) {
      return
    }

    if (draftRef.current === pending.text) {
      clearDraft()
    }

    scope.attachments.clear()
  }, [clearDraft, committed, draftRef, scope.attachments])

  // Hand a worktree off to the controller: open a fresh session anchored there,
  // carrying the composer draft as its first turn. Clearing here means the draft
  // travels to the new session instead of getting stashed under this one.
  const openInWorktree = useCallback(
    (path: string, profile = owner.profile, generation = owner.generation) => {
      const text = draftRef.current
      const before = $startWorkSessionRequest.get()

      requestStartWorkSession(path, text, profile, generation)

      if ($startWorkSessionRequest.get() === before) {
        return
      }

      const accepted = $startWorkSessionRequest.get()

      if (accepted) {
        pendingClearRef.current = {
          generation: accepted.generation,
          profile: accepted.profile,
          text,
          token: accepted.token
        }
      }
    },
    [draftRef, owner.generation, owner.profile]
  )

  // Branch off into a NEW worktree (base = branch name, or current HEAD). A
  // create failure throws back to the row (which toasts) before we touch the
  // draft; a missing cwd / remote backend no-ops (the row hides the affordance).
  const handleBranchOff = useCallback(
    async (branch: string, base?: string) => {
      const repoPath = cwd?.trim()

      const result =
        repoPath &&
        (await startWorkInRepo(repoPath, {
          base,
          branch,
          generation: owner.generation,
          name: branch,
          profile: owner.profile
        }))

      if (result) {
        openInWorktree(result.path, result.profile, result.generation)
      }
    },
    [cwd, openInWorktree, owner.generation, owner.profile]
  )

  // Convert an EXISTING branch into a fresh worktree + session (no new branch).
  // Mirrors handleBranchOff's hand-off: create the worktree, then open a session
  // anchored there carrying the draft.
  const handleConvertBranch = useCallback(
    async (branch: string, path?: null | string, isDefault?: boolean) => {
      if (path?.trim()) {
        openInWorktree(path)

        return
      }

      const repoPath = cwd?.trim()

      if (repoPath && isDefault) {
        const switched = await switchBranchInRepo(repoPath, branch, owner)

        if (switched) {
          openInWorktree(repoPath, switched.profile, switched.generation)
        }

        return
      }

      const result =
        repoPath &&
        (await startWorkInRepo(repoPath, {
          existingBranch: branch,
          generation: owner.generation,
          profile: owner.profile
        }))

      if (result) {
        openInWorktree(result.path, result.profile, result.generation)
      }
    },
    [cwd, openInWorktree, owner]
  )

  const handleListBranches = useCallback(async () => {
    const repoPath = cwd?.trim()

    return repoPath ? listRepoBranches(repoPath, owner) : []
  }, [cwd, owner])

  const handleSwitchBranch = useCallback(
    async (branch: string) => {
      const repoPath = cwd?.trim()

      if (repoPath) {
        await switchBranchInRepo(repoPath, branch, owner)
      }
    },
    [cwd, owner]
  )

  return { handleBranchOff, handleConvertBranch, handleListBranches, handleSwitchBranch, openInWorktree }
}
