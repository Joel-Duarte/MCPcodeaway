export default defineNuxtRouteMiddleware(async (to) => {
  const { loadAuthState } = useAdminApi();
  try {
    const { session } = await loadAuthState();
    const needsLogin = session.admin_auth_required && (!session.authenticated || !session.is_admin);
    if (to.path !== "/login" && needsLogin) {
      return navigateTo("/login");
    }
    if (to.path === "/login" && (!session.admin_auth_required || (session.authenticated && session.is_admin))) {
      return navigateTo("/");
    }
  } catch {
    // Allow navigation to proceed cleanly; the active view will render an actionable banner if gateway is unreachable.
  }
});
