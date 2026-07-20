import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { selectDesktopPaths, selectDesktopPathsForProfile } from '@/lib/desktop-fs'
import { $activeGatewayProfile } from '@/store/profile'

import { RemoteFolderPicker } from './remote-picker'

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(res => {
    resolve = res
  })

  return { promise, resolve }
}

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
    vi.unstubAllGlobals()
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

  it('cancels a generic request across a rapid alpha to beta to alpha swap', async () => {
    renderPicker()
    const pending = selectDesktopPaths({ directories: true })

    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    act(() => {
      $activeGatewayProfile.set('beta')
      $activeGatewayProfile.set('alpha')
    })

    await expect(pending).resolves.toEqual([])
  })

  it('does not paint an old directory listing after an alpha to beta to alpha swap', async () => {
    const listing = deferred<{ entries: Array<{ isDirectory: boolean; name: string; path: string }>; path: string }>()

    const apiMock = vi.fn(() => listing.promise)

    ;(window as unknown as { hermesDesktop: { api: typeof apiMock } }).hermesDesktop.api = apiMock

    renderPicker()
    void selectDesktopPathsForProfile('alpha', { directories: true })

    await vi.waitFor(() => expect(apiMock).toHaveBeenCalled())
    $activeGatewayProfile.set('beta')
    $activeGatewayProfile.set('alpha')
    listing.resolve({
      entries: [{ isDirectory: true, name: 'stale-alpha', path: '/stale-alpha' }],
      path: '/'
    })

    await act(async () => {
      await listing.promise
      await Promise.resolve()
    })

    expect(screen.queryByText('stale-alpha')).toBeNull()
  })
})
