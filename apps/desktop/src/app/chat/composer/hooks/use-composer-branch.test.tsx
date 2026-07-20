import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { $startWorkSessionCommitted, $startWorkSessionRequest, markStartWorkSessionCommitted } from '@/store/projects'

import { MAIN_COMPOSER_SCOPE } from '../scope'

import { useComposerBranch } from './use-composer-branch'

describe('useComposerBranch profile ownership', () => {
  beforeEach(() => {
    $activeGatewayProfile.set('composer-alpha')
    $startWorkSessionRequest.set(null)
    $startWorkSessionCommitted.set(null)
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('does not clear the new profile draft or attachments after an old handoff commits', () => {
    const clearDraft = vi.fn()
    const clearAttachments = vi.spyOn(MAIN_COMPOSER_SCOPE.attachments, 'clear')
    const draftRef = { current: 'same text' }
    const { result } = renderHook(() => useComposerBranch({ clearDraft, cwd: '/repo', draftRef }))

    act(() => result.current.openInWorktree('/repo/worktree'))
    const request = $startWorkSessionRequest.get()

    expect(request).not.toBeNull()

    act(() => {
      markStartWorkSessionCommitted(request?.token ?? 0)
      $activeGatewayProfile.set('composer-beta')
    })

    expect(clearDraft).not.toHaveBeenCalled()
    expect(clearAttachments).not.toHaveBeenCalled()
  })
})
