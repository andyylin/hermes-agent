import { describe, expect, test } from 'vitest'

import { buildProfileBridgePayload } from './profile-bridge'

describe('buildProfileBridgePayload', () => {
  test('returns the local Hermes home used by Desktop plugin discovery', () => {
    expect(buildProfileBridgePayload('coder', '/Users/andy/.hermes')).toEqual({
      profile: 'coder',
      hermes_home: '/Users/andy/.hermes'
    })
  })
})