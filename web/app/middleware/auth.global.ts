export default defineNuxtRouteMiddleware(async (to) => {
  const { loadAuthState } = useAdminApi();
  try {
    const { session } = await loadAuthState();
    const needsLogin =
      session.admin_auth_required &&
      (!session.authenticated || !session.can_access_admin_console);
    if (to.path !== "/login" && needsLogin) {
      return navigateTo({
        path: "/login",
        query:
          to.query.auth_error === "not_authorized"
            ? { auth_error: "not_authorized" }
            : undefined,
      });
    }
    if (
      to.path === "/login" &&
      (!session.admin_auth_required ||
        (session.authenticated && session.can_access_admin_console))
    ) {
      return navigateTo("/");
    }
  } catch {
    // Allow navigation to proceed cleanly; the active view will render an actionable banner if gateway is unreachable.
  }
});
