import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $previewTarget } from '@/store/preview'
import { $activeGatewayProfile, $activeGatewayProfileGeneration } from '@/store/profile'

const { normalizeOrLocalPreviewTarget } = vi.hoisted(() => ({
  normalizeOrLocalPreviewTarget: vi.fn()
}))

vi.mock('@/lib/local-preview', () => ({ normalizeOrLocalPreviewTarget }))

import { PreviewStatusRow } from './preview-row'

describe('PreviewStatusRow', () => {
  beforeEach(() => {
    normalizeOrLocalPreviewTarget.mockReset()
    $activeGatewayProfile.set('alpha')
    $activeGatewayProfileGeneration.set(1)
    $previewTarget.set(null)
  })

  afterEach(() => {
    cleanup()
  })

  it('keeps the preview tooltip label inline inside the portaled decoration', async () => {
    const view = render(
      <PreviewStatusRow
        item={{ cwd: 'C:\\repo', id: 'preview.html', label: 'preview.html', target: 'preview.html' }}
        onDismiss={() => undefined}
      />
    )

    fireEvent.pointerMove(screen.getByText('preview.html'), { pointerType: 'mouse' })
    await screen.findByRole('tooltip')

    const content = globalThis.document.querySelector<HTMLElement>('[data-slot="tooltip-content"]')
    const label = content?.firstElementChild?.firstElementChild

    expect(content).not.toBeNull()
    expect(view.container.contains(content)).toBe(false)
    expect(label?.classList.contains('inline-flex')).toBe(true)
    expect(label?.classList.contains('flex')).toBe(false)
  })

  it('does not publish a preview when ownership changes between helper and caller continuations', async () => {
    let resolve!: (value: {
      kind: 'file'
      label: string
      path: string
      previewKind: 'text'
      source: string
      url: string
    }) => void

    const normalized = new Promise<{
      kind: 'file'
      label: string
      path: string
      previewKind: 'text'
      source: string
      url: string
    }>(done => {
      resolve = done
    })

    normalizeOrLocalPreviewTarget.mockReturnValue(normalized)
    render(
      <PreviewStatusRow
        item={{ cwd: '/alpha', id: 'notes.txt', label: 'notes.txt', target: 'notes.txt' }}
        onDismiss={() => undefined}
      />
    )

    fireEvent.click(screen.getByText('notes.txt'), { ctrlKey: true })

    const switchAfterHelper = normalized.then(() => {
      $activeGatewayProfile.set('beta')
      $activeGatewayProfileGeneration.set(2)
    })

    await act(async () => {
      resolve({
        kind: 'file',
        label: 'notes.txt',
        path: '/alpha/notes.txt',
        previewKind: 'text',
        source: 'notes.txt',
        url: 'file:///alpha/notes.txt'
      })
      await switchAfterHelper
    })

    await waitFor(() => expect($previewTarget.get()).toBeNull())
  })
})
