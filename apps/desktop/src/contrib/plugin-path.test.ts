import { afterEach, expect, test, vi } from 'vitest'

import { getLocalDesktopPluginsDir } from './plugin-path'

const originalDesktop = window.hermesDesktop

afterEach(() => {
  Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: originalDesktop })
})

test('resolves the plugin directory from the Desktop-local Hermes home', async () => {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      profile: {
        get: vi.fn().mockResolvedValue({ profile: null, hermes_home: '/Users/andy/.hermes/' })
      }
    }
  })

  await expect(getLocalDesktopPluginsDir()).resolves.toBe('/Users/andy/.hermes/desktop-plugins')
})

test('fails closed when an older Desktop bridge omits the local Hermes home', async () => {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      profile: {
        get: vi.fn().mockResolvedValue({ profile: null })
      }
    }
  })

  await expect(getLocalDesktopPluginsDir()).rejects.toThrow('Desktop bridge did not provide a local Hermes home')
})
