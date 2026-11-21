export interface AdminUserProfile {
  sub?: string;
  email?: string;
  tenant_id?: string;
  roles?: string[];
  groups?: string[];
  client_groups?: string[];
  iss?: string;
}

export interface AdminSessionState {
  authenticated: boolean;
  user: AdminUserProfile | null;
  is_admin: boolean;
  can_access_admin_console: boolean;
  admin_auth_required: boolean;
}

export interface AdminAuthConfig {
  auth_mode: string;
  profile: string;
  admin_auth_required: boolean;
  oidc_enabled: boolean;
  oidc_issuer?: string;
  dev_auth_enabled: boolean;
  token_login_enabled: boolean;
  dev_personas: string[];
}

export interface RuntimeShellMeta {
  profile: string;
  app_env: string;
  catalog_version: string;
}

function buildUrl(apiBase: string, path: string): string {
  const base = apiBase.replace(/\/+$/, "");
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return base ? `${base}${normalized}` : normalized;
}

export function resolveFuzzyModel(query: string, availableModels: string[]): string | null {
  const q = query.trim().toLowerCase();
  if (!q || !availableModels.length) return null;

  const exact = availableModels.find((m) => m.toLowerCase() === q);
  if (exact) return exact;

  const prefix = availableModels
    .filter((m) => m.toLowerCase().startsWith(q))
    .sort((a, b) => a.length - b.length);
  if (prefix.length) return prefix[0] ?? null;

  const substring = availableModels
    .filter((m) => m.toLowerCase().includes(q))
    .sort((a, b) => {
      const idxDiff = a.toLowerCase().indexOf(q) - b.toLowerCase().indexOf(q);
      return idxDiff !== 0 ? idxDiff : a.length - b.length;
    });
  if (substring.length) return substring[0] ?? null;

  const fuzzy = availableModels
    .filter((m) => {
      const hay = m.toLowerCase();
      let pos = 0;
      for (const ch of q) {
        pos = hay.indexOf(ch, pos);
        if (pos === -1) return false;
        pos += 1;
      }
      return true;
    })
    .sort((a, b) => a.length - b.length);
  return fuzzy[0] ?? null;
}

export function extractErrorMessage(err: unknown): string {
  if (!err || typeof err !== "object") return "Request failed.";
  const rec = err as Record<string, unknown>;
  const data = rec.data as Record<string, unknown> | undefined;
  if (data && typeof data.detail === "string" && data.detail) {
    return data.detail;
  }
  if (typeof rec.statusMessage === "string" && rec.statusMessage) {
    return rec.statusMessage;
  }
  if (typeof rec.message === "string" && rec.message) {
    return rec.message;
  }
  return "Request failed.";
}

export function useAdminApi() {
  const config = useRuntimeConfig();
  const router = useRouter();

  const session = useState<AdminSessionState | null>("admin-session", () => null);
  const authConfig = useState<AdminAuthConfig | null>("admin-auth-config", () => null);
  const bearerToken = useState<string>("admin-bearer-token", () => "");
  const runtimeMeta = useState<RuntimeShellMeta>("admin-runtime-meta", () => ({
    profile: "main",
    app_env: "dev",
    catalog_version: "—",
  }));

  async function apiFetch<T>(
    path: string,
    opts: Parameters<typeof $fetch<T>>[1] = {},
    skipRedirect = false
  ): Promise<T> {
    const apiBase = String(config.public.apiBase || "").replace(/\/+$/, "");
    const url = buildUrl(apiBase, path);
    const headers: Record<string, string> = {
      ...(opts?.headers as Record<string, string> | undefined),
    };
    if (bearerToken.value && !headers.Authorization) {
      headers.Authorization = `Bearer ${bearerToken.value}`;
    }
    try {
      return await $fetch<T>(url, {
        baseURL: apiBase ? undefined : "/",
        timeout: 25000,
        ...opts,
        headers,
        credentials: "include",
      });
    } catch (err: unknown) {
      const status =
        typeof err === "object" && err !== null
          ? ((err as { statusCode?: number; status?: number }).statusCode ??
            (err as { status?: number }).status)
          : undefined;
      if (
        !skipRedirect &&
        (status === 401 || status === 403) &&
        session.value?.admin_auth_required &&
        router.currentRoute.value.path !== "/login"
      ) {
        session.value = {
          authenticated: false,
          user: null,
          is_admin: false,
          can_access_admin_console: false,
          admin_auth_required: true,
        };
        await router.push("/login");
      }
      throw err;
    }
  }

  async function loadAuthState(force = false): Promise<{
    session: AdminSessionState;
    authConfig: AdminAuthConfig;
  }> {
    if (!force && session.value && authConfig.value) {
      return { session: session.value, authConfig: authConfig.value };
    }
    const [cfgRes, initialSess] = await Promise.all([
      apiFetch<AdminAuthConfig>("/auth/admin/config", {}, true),
      apiFetch<AdminSessionState>("/auth/admin/session", {}, true),
    ]);
    let sessRes = initialSess;
    if (!sessRes.authenticated && cfgRes.dev_auth_enabled) {
      const externalOidcBase = (cfgRes.oidc_issuer || "http://127.0.0.1:8001/oidc").replace(/\/+$/, "");
      try {
        const oidcToken = await $fetch<{ access_token: string }>(
          `${externalOidcBase}/token`,
          {
            method: "POST",
            body: { persona: "gateway_admin" },
          }
        );
        if (oidcToken?.access_token) {
          bearerToken.value = oidcToken.access_token;
          await apiFetch(
            "/auth/admin/token-login",
            {
              method: "POST",
              body: { token: oidcToken.access_token },
            },
            true
          );
          const [verifiedSession, userInfo] = await Promise.all([
            apiFetch<AdminSessionState>("/auth/admin/session", {}, true),
            $fetch<AdminUserProfile>(`${externalOidcBase}/userinfo`, {
              headers: { Authorization: `Bearer ${oidcToken.access_token}` },
            }),
          ]);
          sessRes = {
            ...verifiedSession,
            user: {
              ...(verifiedSession.user || {}),
              ...userInfo,
            },
          };
        }
      } catch {
        try {
          sessRes = await devLogin("gateway_admin");
        } catch {
        }
      }
    }
    authConfig.value = cfgRes;
    session.value = sessRes;
    runtimeMeta.value = {
      ...runtimeMeta.value,
      profile: cfgRes.profile || runtimeMeta.value.profile,
    };
    return { session: sessRes, authConfig: cfgRes };
  }

  async function devLogin(persona: string): Promise<AdminSessionState> {
    const res = await apiFetch<{
      authenticated: boolean;
      access_token?: string;
      user: AdminUserProfile;
    }>(
      "/auth/admin/dev-login",
      {
        method: "POST",
        body: { persona },
      },
      true
    );
    if (res.access_token) {
      bearerToken.value = res.access_token;
    }
    const updated = await apiFetch<AdminSessionState>("/auth/admin/session", {}, true);
    session.value = updated;
    return updated;
  }

  async function tokenLogin(token: string): Promise<AdminSessionState> {
    await apiFetch<{ authenticated: boolean; user: AdminUserProfile }>(
      "/auth/admin/token-login",
      {
        method: "POST",
        body: { token },
      },
      true
    );
    bearerToken.value = "";
    const updated = await apiFetch<AdminSessionState>("/auth/admin/session", {}, true);
    session.value = updated;
    return updated;
  }

  async function logout(): Promise<void> {
    try {
      await apiFetch("/auth/admin/logout", { method: "POST" }, true);
    } finally {
      bearerToken.value = "";
      session.value = null;
      await loadAuthState(true);
      await router.push("/login");
    }
  }

  function oidcLoginUrl(): string {
    return buildUrl(
      String(config.public.apiBase || ""),
      "/auth/admin/login?next_url=/dashboard/"
    );
  }

  function setRuntimeMeta(partial: Partial<RuntimeShellMeta>): void {
    runtimeMeta.value = {
      ...runtimeMeta.value,
      ...partial,
    };
  }

  return {
    session,
    authConfig,
    bearerToken,
    runtimeMeta,
    apiFetch,
    loadAuthState,
    devLogin,
    tokenLogin,
    logout,
    oidcLoginUrl,
    setRuntimeMeta,
  };
}
