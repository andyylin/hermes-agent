import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { $managedProjectCreateAvailable, $projectDialog, closeProjectDialog } from '@/store/projects'

import { ProjectDialog } from './project-dialog'

vi.mock('@/lib/desktop-fs', () => ({
  desktopDefaultCwd: vi.fn().mockResolvedValue({ cwd: '/workspace' }),
  selectDesktopPaths: vi.fn(),
  writeDesktopFileText: vi.fn()
}))

vi.mock('@/store/gateway', () => ({
  $activeGatewayProfile: atom('default'),
  activeGateway: vi.fn(),
  ensureActiveGatewayOpen: vi.fn()
}))

const fs = await import('@/lib/desktop-fs')
const selectDesktopPaths = vi.mocked(fs.selectDesktopPaths)
const gateway = await import('@/store/gateway')
const activeGateway = vi.mocked(gateway.activeGateway)
const activeGatewayProfile = gateway.$activeGatewayProfile

function renderCreateDialog(request: ReturnType<typeof vi.fn>) {
  activeGateway.mockReturnValue({ connectionState: 'open', request } as never)
  $projectDialog.set({ mode: 'create' })
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <ProjectDialog />
    </I18nProvider>
  )
}

function projectResponse(method: string) {
  const project = {
    folders: [{ path: '/profile/projects/demo' }],
    id: 'p_demo',
    name: 'Demo',
    primary_path: '/profile/projects/demo'
  }
  if (method === 'projects.create' || method === 'projects.create_managed') {
    return { project }
  }
  if (method === 'projects.tree') {
    return { active_id: 'p_demo', projects: [], scoped_session_ids: [], session_assignments: {} }
  }
  return { active_id: 'p_demo', projects: [project] }
}

describe('ProjectDialog project location', () => {
  let profileNumber = 0

  beforeEach(() => {
    vi.clearAllMocks()
    $managedProjectCreateAvailable.set(null)
    closeProjectDialog()
    profileNumber += 1
    activeGatewayProfile.set(`dialog-test-${profileNumber}`)
  })

  afterEach(() => {
    cleanup()
    closeProjectDialog()
  })

  it('creates a profile-managed folder by default', async () => {
    const request = vi.fn(async (method: string) => projectResponse(method))
    renderCreateDialog(request)

    fireEvent.change(screen.getByPlaceholderText('e.g. Skunkworks'), { target: { value: 'Demo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        'projects.create_managed',
        expect.not.objectContaining({ folders: expect.anything(), primary_path: expect.anything() })
      )
    })
  })

  it('keeps existing-folder creation behind an explicit picker choice', async () => {
    const request = vi.fn(async (method: string) => projectResponse(method))
    selectDesktopPaths.mockResolvedValue(['/existing/demo'])
    renderCreateDialog(request)

    fireEvent.change(screen.getByPlaceholderText('e.g. Skunkworks'), { target: { value: 'Demo' } })
    fireEvent.click(screen.getByRole('radio', { name: /Use existing/ }))
    expect((screen.getByRole('button', { name: 'Create' }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Add folder' }))
    await screen.findByText('/existing/demo')
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith(
        'projects.create',
        expect.objectContaining({ folders: ['/existing/demo'], name: 'Demo' })
      )
    })
  })

  it('cannot be dismissed while project creation is in flight', async () => {
    let resolveCreate: ((value: unknown) => void) | undefined
    const request = vi.fn((method: string) => {
      if (method === 'projects.create_managed') {
        return new Promise(resolve => {
          resolveCreate = resolve
        })
      }
      return Promise.resolve(projectResponse(method))
    })
    renderCreateDialog(request)

    const input = screen.getByPlaceholderText('e.g. Skunkworks')
    fireEvent.change(input, { target: { value: 'Demo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(resolveCreate).toBeTypeOf('function'))

    fireEvent.keyDown(input, { key: 'Escape' })
    expect(screen.getByRole('dialog')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(screen.getByRole('dialog')).not.toBeNull()

    await act(async () => resolveCreate?.(projectResponse('projects.create_managed')))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('does not let an old submission close a newly opened dialog', async () => {
    let resolveCreate: ((value: unknown) => void) | undefined
    const request = vi.fn((method: string) => {
      if (method === 'projects.create_managed') {
        return new Promise(resolve => {
          resolveCreate = resolve
        })
      }
      return Promise.resolve(projectResponse(method))
    })
    renderCreateDialog(request)

    fireEvent.change(screen.getByPlaceholderText('e.g. Skunkworks'), { target: { value: 'First' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(resolveCreate).toBeTypeOf('function'))

    act(() => {
      closeProjectDialog()
      $projectDialog.set({ mode: 'create', name: 'Second' })
    })
    await screen.findByDisplayValue('Second')
    await act(async () => resolveCreate?.(projectResponse('projects.create_managed')))

    await waitFor(() => expect($projectDialog.get()).toEqual({ mode: 'create', name: 'Second' }))
    expect(screen.getByRole('dialog')).not.toBeNull()
  })

  it('does not let a submission from the previous profile close the current dialog', async () => {
    let resolveCreate: ((value: unknown) => void) | undefined
    const request = vi.fn((method: string) => {
      if (method === 'projects.create_managed') {
        return new Promise(resolve => {
          resolveCreate = resolve
        })
      }
      return Promise.resolve(projectResponse(method))
    })
    renderCreateDialog(request)

    fireEvent.change(screen.getByPlaceholderText('e.g. Skunkworks'), { target: { value: 'Profile A' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(resolveCreate).toBeTypeOf('function'))

    act(() => {
      activeGateway.mockReturnValue({ connectionState: 'open', request: vi.fn() } as never)
      activeGatewayProfile.set('profile-b')
    })
    await waitFor(() => expect(screen.getByPlaceholderText('e.g. Skunkworks')).toHaveProperty('value', ''))

    await act(async () => resolveCreate?.(projectResponse('projects.create_managed')))

    expect($projectDialog.get()).toEqual({ mode: 'create' })
    expect(screen.getByRole('dialog')).not.toBeNull()
  })

  it('falls back to existing-folder mode when managed creation is unavailable', () => {
    $managedProjectCreateAvailable.set(false)
    const request = vi.fn(async (method: string) => projectResponse(method))
    renderCreateDialog(request)

    const managed = screen.getByRole('radio', { name: /Create a folder/ })
    const existing = screen.getByRole('radio', { name: /Use existing/ })

    expect((managed as HTMLButtonElement).disabled).toBe(true)
    expect(existing.getAttribute('aria-checked')).toBe('true')
  })
})
