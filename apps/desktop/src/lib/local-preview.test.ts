import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { normalizeOrLocalPreviewTarget } from '@/lib/local-preview'
import { $activeGatewayProfile, $activeGatewayProfileGeneration } from '@/store/profile'
import { $connection } from '@/store/session'

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

  afterEach(() => {
    $connection.set(null)
  })

  it('drops an alpha preview after an alpha to beta to alpha generation swap', async () => {
    const api = vi.fn()

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
      value: { api, normalizePreviewTarget: vi.fn(() => normalized.promise) }
    })
    $connection.set({ mode: 'remote', profile: 'alpha' } as never)

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
    expect(api).not.toHaveBeenCalled()
  })
})
