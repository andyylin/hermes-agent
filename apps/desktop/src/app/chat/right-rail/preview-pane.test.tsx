import { act, cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { $connection } from '@/store/session'

import { LocalFilePreview } from './preview-file'
import { PreviewPane } from './preview-pane'

describe('PreviewPane console state', () => {
  let originalDesktop: typeof window.hermesDesktop

  beforeEach(() => {
    originalDesktop = window.hermesDesktop
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) =>
      window.setTimeout(() => callback(Date.now()), 0)
    )
    vi.stubGlobal('cancelAnimationFrame', (id: number) => window.clearTimeout(id))
  })

  afterEach(() => {
    cleanup()
    $activeGatewayProfile.set('default')
    $connection.set(null)
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: originalDesktop })
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('does not publish an old file read after an alpha to beta to alpha generation swap', async () => {
    let resolveAlpha!: (value: { byteSize: number; path: string; text: string }) => void

    const oldAlphaRead = new Promise<{ byteSize: number; path: string; text: string }>(resolve => {
      resolveAlpha = resolve
    })

    const readFileText = vi
      .fn()
      .mockReturnValueOnce(oldAlphaRead)
      .mockResolvedValueOnce({ byteSize: 4, path: '/same.txt', text: 'beta' })
      .mockResolvedValueOnce({ byteSize: 11, path: '/same.txt', text: 'fresh alpha' })

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        getConnection: vi.fn(async (profile: string) => ({ mode: 'local', profile })),
        gitRoot: vi.fn(async () => null),
        readFileText
      }
    })

    $activeGatewayProfile.set('alpha')
    $connection.set({ mode: 'local', profile: 'alpha' } as never)

    const rendered = render(
      <LocalFilePreview
        reloadKey={0}
        target={{
          kind: 'file',
          label: 'same.txt',
          path: '/same.txt',
          previewKind: 'text',
          source: '/same.txt',
          url: 'file:///same.txt'
        }}
      />
    )

    await waitFor(() => expect(readFileText).toHaveBeenCalledTimes(1))

    await act(async () => {
      $activeGatewayProfile.set('beta')
      $connection.set({ mode: 'local', profile: 'beta' } as never)
    })

    await waitFor(() => expect(readFileText).toHaveBeenCalledTimes(2))

    await act(async () => {
      $activeGatewayProfile.set('alpha')
      $connection.set({ mode: 'local', profile: 'alpha' } as never)
    })

    await waitFor(() => expect(rendered.container.textContent).toContain('fresh alpha'))

    await act(async () => {
      resolveAlpha({ byteSize: 9, path: '/same.txt', text: 'stale alpha' })
      await oldAlphaRead
    })

    expect(rendered.container.textContent).toContain('fresh alpha')
    expect(rendered.container.textContent).not.toContain('stale alpha')
  })

  it('does not watch backend-only remote filesystem previews locally', async () => {
    const watchPreviewFile = vi.fn(async () => ({ id: 'watch-1', path: '/remote/file.txt' }))
    const onPreviewFileChanged = vi.fn(() => vi.fn())
    $connection.set({ mode: 'remote' } as never)
    vi.stubGlobal('window', {
      ...window,
      hermesDesktop: {
        onPreviewFileChanged,
        watchPreviewFile
      }
    })

    await act(async () => {
      render(
        <PreviewPane
          setTitlebarToolGroup={vi.fn()}
          target={{
            kind: 'file',
            label: 'file.txt',
            path: '/remote/file.txt',
            previewKind: 'text',
            source: '/remote/file.txt',
            url: 'file:///remote/file.txt'
          }}
        />
      )
    })

    expect(watchPreviewFile).not.toHaveBeenCalled()
    expect(onPreviewFileChanged).not.toHaveBeenCalled()
  })

  it('does not rebuild the pane titlebar group for streamed console logs', async () => {
    const setTitlebarToolGroup = vi.fn()

    let rendered!: ReturnType<typeof render>
    await act(async () => {
      rendered = render(
        <PreviewPane
          setTitlebarToolGroup={setTitlebarToolGroup}
          target={{
            kind: 'url',
            label: 'Preview',
            source: 'http://localhost:5174',
            url: 'http://localhost:5174'
          }}
        />
      )
    })

    const initialCalls = setTitlebarToolGroup.mock.calls.length
    const webview = rendered.container.querySelector('webview')

    expect(webview).toBeInstanceOf(HTMLElement)

    act(() => {
      webview?.dispatchEvent(
        Object.assign(new Event('console-message'), {
          level: 0,
          message: 'streamed log line',
          sourceId: 'http://localhost:5174/src/main.tsx'
        })
      )
    })

    expect(setTitlebarToolGroup).toHaveBeenCalledTimes(initialCalls)
  })
})
