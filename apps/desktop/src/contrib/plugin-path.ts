export async function getLocalDesktopPluginsDir() {
  const { hermes_home } = await window.hermesDesktop.profile.get()
  const home = hermes_home.replace(/[\\/]+$/, '')

  return `${home}/desktop-plugins`
}
