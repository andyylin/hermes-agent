import { beforeEach, describe, expect, it, vi } from 'vitest'

import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { $activeGatewayProfile, $activeGatewayProfileGeneration } from '@/store/profile'

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

describe('profile-bound local preview normalization', () => {
  beforeEach(() => {
    $activeGatewayProfile.set('alpha')
    $activeGatewayProfileGeneration.set(1)
  })

  it('drops an alpha preview after an alpha to beta to alpha generation swap', async () => {
    const normalized = deferred<{
      kind: 'file'
      label: string
      path: string
      previewKind: 'text'
      source: string
      url: string
    }>()

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { normalizePreviewTarget: vi.fn(() => normalized.promise) }
    })

    const result = normalizeOrLocalPreviewTarget('/alpha/notes.txt')
    $activeGatewayProfile.set('beta')
    $activeGatewayProfileGeneration.set(2)
    $activeGatewayProfile.set('alpha')
    $activeGatewayProfileGeneration.set(3)
    normalized.resolve({
      kind: 'file',
      label: 'notes.txt',
      path: '/alpha/notes.txt',
      previewKind: 'text',
      source: '/alpha/notes.txt',
      url: 'file:///alpha/notes.txt'
    })

    await expect(result).resolves.toBeNull()
  })
})
