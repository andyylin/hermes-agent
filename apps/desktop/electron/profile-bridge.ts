export function buildProfileBridgePayload(profile: string | null, hermesHome: string) {
  return {
    profile,
    hermes_home: hermesHome
  }
}