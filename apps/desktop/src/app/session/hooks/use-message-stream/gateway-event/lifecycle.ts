import type { HermesSkin } from '@hermes/shared/skin'

import { translateNow } from '@/i18n'
import {
  notifyCronChanged,
  notifyPairingChanged,
  notifyPetChanged,
  notifyPlatformsChanged,
  notifySessionsChanged,
  type PetChangeMeta,
  setChangeEventsAvailable
} from '@/store/live-sync'
import { dispatchNativeNotification } from '@/store/native-notifications'
import { dropSessionState, unbindTileRuntime } from '@/store/session-states'
// Leaf import (not the `@/themes` barrel) to avoid pulling the ThemeProvider
// module graph into the gateway event hot path.
import { ingestBackendSkin } from '@/themes/backend-sync'

import type { GatewayEventContext } from './types'

/** gateway.ready / skin.changed / change-watcher broadcasts / session.reclaimed. */
export function handleLifecycleEvent(ctx: GatewayEventContext): boolean {
  const { deps, event, payload, fromActiveSource } = ctx

  if (event.type === 'gateway.ready') {
    // Seed the active skin into the desktop theme registry without applying,
    // so a fresh connect never overrides the user's persisted desktop theme.
    ingestBackendSkin((payload as { skin?: HermesSkin } | undefined)?.skin, { apply: false })
    // Backends with the change watcher broadcast pet/cron/sessions change
    // events; consumers demote their legacy polls to slow backstops.
    setChangeEventsAvailable(Boolean((payload as { change_events?: boolean } | undefined)?.change_events))

    return true
  }

  if (event.type === 'skin.changed') {
    // A runtime skin switch (Hermes activating an authored skin, or `/skin`
    // on another surface). Only the active source+profile's change repaints.
    if (fromActiveSource()) {
      ingestBackendSkin(payload as HermesSkin | undefined, { apply: true })
    }

    return true
  }

  if (
    event.type === 'pet.changed' ||
    event.type === 'cron.changed' ||
    event.type === 'sessions.changed' ||
    event.type === 'platforms.changed' ||
    event.type === 'pairing.changed'
  ) {
    // Change-watcher broadcasts (server._broadcast_watched_changes): the
    // backend's on-disk signature moved. Route to the live-sync ticks the
    // former pollers now subscribe to. Only the active source+profile's
    // changes apply — background profile sockets (and other connections'
    // gateways) watch their own homes.
    if (fromActiveSource()) {
      if (event.type === 'pet.changed') {
        notifyPetChanged(payload as PetChangeMeta | undefined)
      } else if (event.type === 'cron.changed') {
        notifyCronChanged()
      } else if (event.type === 'platforms.changed') {
        notifyPlatformsChanged()
      } else if (event.type === 'pairing.changed') {
        notifyPairingChanged()
      } else {
        notifySessionsChanged()
      }
    }

    return true
  }

  if (event.type === 'session.message.created') {
    // Out-of-band append into a STORED session (cron delivering
    // `deliver=origin` output back into the Desktop/TUI/CLI conversation
    // that created the job — see `cron/session_delivery.py`). The backend
    // addresses this by stored id, a different keyspace from the runtime
    // `sid` this event's own top-level `session_id` may or may not carry
    // (see `tui_gateway.server.broadcast_stored_session_event`), so read the
    // stored id straight out of the payload rather than trusting `ctx.sessionId`.
    const messagePayload = payload as
      | { job_name?: string; preview?: string; session_id?: string }
      | undefined
    const storedSessionId = String(messagePayload?.session_id ?? '')

    // The row is already durably appended regardless of any of this — refresh
    // the session list (unread indicator) and fire a native notification only
    // when the target isn't the session already on screen.
    notifySessionsChanged()

    if (storedSessionId) {
      const runtimeId = deps.runtimeIdByStoredSessionIdRef.current.get(storedSessionId)
      // Falls back to the stored id itself when this window never opened that
      // session (no runtime mapping yet): comparing a stored id against the
      // active RUNTIME session id can never accidentally match, which is the
      // correct "off-screen" answer for a session this window isn't showing.
      const notificationSessionId = runtimeId ?? storedSessionId

      dispatchNativeNotification({
        body: messagePayload?.preview || translateNow('notifications.native.sessionMessageBody'),
        kind: 'sessionMessage',
        sessionId: notificationSessionId,
        title: messagePayload?.job_name || translateNow('notifications.native.sessionMessageTitle')
      })
    }

    return true
  }

  if (event.type === 'session.reclaimed') {
    // The backend reclaimed a live session we may still be holding (idle
    // TTL, LRU cap, or the WS-orphan reap). Without this the runtime id
    // stays cached until something fails against it, which reads as the
    // session vanishing rather than being reclaimed. Drop the cached state
    // now — the stored row is untouched, so the sidebar keeps the
    // conversation and reopening it resumes from the DB.
    const reclaimedRuntimeId = String((payload as { session_id?: string } | undefined)?.session_id ?? '')

    if (reclaimedRuntimeId) {
      dropSessionState(reclaimedRuntimeId)
      // A tile bound to the reclaimed runtime would otherwise render an
      // empty transcript forever: its view reads $sessionStates[runtime]
      // (just dropped) and its resume effect is gated on !runtimeId, so a
      // bound tile never re-resumes (#82620). Unbind it so the effect
      // refires against the intact stored session — and purge the wiring
      // cache's entry, or resumeTile's warm path would hand the dead
      // runtime straight back instead of cold-resuming a live one.
      unbindTileRuntime(reclaimedRuntimeId)
      deps.sessionStateByRuntimeIdRef.current.delete(reclaimedRuntimeId)
    }

    // The row's ended_at moved, so refresh the lists that render it.
    notifySessionsChanged()

    return true
  }

  return false
}
