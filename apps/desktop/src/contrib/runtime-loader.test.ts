import { afterEach, expect, test, vi } from 'vitest'

import { discoverRuntimePlugins } from './runtime-loader'

const originalDesktop = window.hermesDesktop

afterEach(() => {
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: originalDesktop })
})

test('discovers disk plugins from the Desktop-local Hermes home', async () => {
  const readDir = vi.fn().mockResolvedValue({ entries: [] })
  const desktop = {
    profile: {
      get: vi.fn().mockResolvedValue({
        profile: null,
        hermes_home: '/Users/andy/.hermes'
      })
    },
    readDir
  }

  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: desktop })

  await discoverRuntimePlugins()

  expect(readDir).toHaveBeenCalledWith('/Users/andy/.hermes/desktop-plugins')
  expect(readDir).not.toHaveBeenCalledWith('/home/pi/.hermes/desktop-plugins')
})
