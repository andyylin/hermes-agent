import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { selectDesktopPathsForProfile } from '@/lib/desktop-fs'
import { $activeGatewayProfile } from '@/store/profile'

import { RemoteFolderPicker } from './remote-picker'

function renderPicker() {
  return render(
    <I18nProvider configClient={null}>
      <RemoteFolderPicker />
    </I18nProvider>
  )
}

describe('RemoteFolderPicker profile lifecycle', () => {
  beforeEach(() => {
    $activeGatewayProfile.set('alpha')
    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = {
      api: vi.fn(async () => ({ entries: [], path: '/' })),
      getConnection: vi.fn(async (profile: string) => ({ mode: 'remote', profile })),
      selectPaths: vi.fn(async () => [])
    }
  })

  afterEach(() => {
    cleanup()
    delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
  })

  it('settles the previous caller when a new request replaces it', async () => {
    renderPicker()

    const first = selectDesktopPathsForProfile('alpha', { directories: true })
    const second = selectDesktopPathsForProfile('alpha', { directories: true })

    await expect(first).resolves.toEqual([])

    act(() => $activeGatewayProfile.set('beta'))
    await expect(second).resolves.toEqual([])
  })

  it('cancels the pending request on profile switch and unmount', async () => {
    const view = renderPicker()
    const switched = selectDesktopPathsForProfile('alpha', { directories: true })

    act(() => $activeGatewayProfile.set('beta'))
    await expect(switched).resolves.toEqual([])

    act(() => $activeGatewayProfile.set('alpha'))
    const unmounted = selectDesktopPathsForProfile('alpha', { directories: true })
    view.unmount()

    await expect(unmounted).resolves.toEqual([])
  })
})
