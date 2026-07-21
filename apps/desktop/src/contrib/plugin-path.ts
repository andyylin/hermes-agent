export async function getLocalDesktopPluginsDir() {
  const { hermes_home } = await window.hermesDesktop.profile.get()

  if (typeof hermes_home !== 'string' || !hermes_home.trim()) {
    throw new Error('Desktop bridge did not provide a local Hermes home')
  }

  const home = hermes_home.replace(/[\\/]+$/, '')

  return `${home}/desktop-plugins`
}
