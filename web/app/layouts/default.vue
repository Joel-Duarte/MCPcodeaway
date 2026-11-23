<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";

const route = useRoute();
const currentPath = computed(() => route.path.replace(/\/+$/, "") || "/");
const { session, loadAuthState, logout } = useAdminApi();
const { initAgentBridge } = useUiAgentBridge();
const { refreshStatus: refreshDemoStatus } = useDemoTraffic();
const { connect: connectWs, disconnect: disconnectWs } = useLiveTelemetry();
const mobileNavigationOpen = ref(false);
const sessionReady = ref(false);
const loggingOut = ref(false);
const { selectedRange } = useReportRange();
const isReportPage = computed(() => currentPath.value === "/analytics" ||
  (currentPath.value === "/model-performance" && route.query.tab !== "discovery"));

const navigation = [
  {
    label: "Overview & Live Runtime",
    to: "/",
    group: "Operate",
    icon: "overview",
  },
  {
    label: "Gateway Usage",
    to: "/analytics",
    group: "Operate",
    icon: "analytics",
  },
  {
    label: "Traces & Timeline",
    to: "/traces",
    group: "Operate",
    icon: "traces",
  },
  {
    label: "Model Performance",
    to: "/model-performance",
    group: "Optimize",
    icon: "models",
  },
  {
    label: "Business Value",
    to: "/business-value",
    group: "Optimize",
    icon: "analytics",
  },
  {
    label: "Upstreams Catalog",
    to: "/upstreams",
    group: "Govern",
    icon: "upstreams",
  },
  {
    label: "Operations Recovery",
    to: "/operations",
    group: "Operate",
    icon: "traces",
  },
  {
    label: "Audit & Compliance",
    to: "/audit",
    group: "Govern",
    icon: "audit",
  },
  {
    label: "Onboarding",
    to: "/onboarding",
    group: "Configure",
    icon: "onboarding",
  },
  {
    label: "Settings & Alerts",
    to: "/settings",
    group: "Configure",
    icon: "settings",
  },
  {
    label: "Demo Control",
    to: "/demo",
    group: "Demo",
    icon: "demo",
  },
] as const;

const groupedNavigation = computed(() => {
  const groups = ["Operate", "Optimize", "Govern", "Configure", "Demo"] as const;
  const visibleNavigation = navigation.filter((item) => {
    const canViewOperations = item.to !== "/operations" || session.value?.is_admin ||
      session.value?.is_viewer;
    return canViewOperations;
  });
  return groups.map((group) => ({
    name: group,
    items: visibleNavigation.filter((item) => item.group === group),
  })).filter((group) => group.items.length > 0);
});

const activeItem = computed(
  () => navigation.find((item) => item.to === currentPath.value) || navigation[0],
);
const isLoginRoute = computed(() => route.path === "/login");
const adminIdentity = computed(() => {
  const user = session.value?.user;
  const value = user?.email || user?.sub || user?.tenant_id;
  if (typeof value === "string" && value.trim()) return value;
  if (session.value?.authenticated) return "Authenticated administrator";
  return session.value?.admin_auth_required ? "Sign-in required" : "Local admin session";
});
const adminRole = computed(() => {
  const roles = session.value?.user?.roles || [];
  if (roles.some((role) => role.toLowerCase().includes("admin")) || session.value?.is_admin) return "Administrator";
  if (session.value?.is_approver) return "Approver";
  if (roles.some((role) => role.toLowerCase().includes("viewer"))) return "Viewer";
  if (roles.length) return roles.join(" · ");
  return session.value?.can_access_admin_console ? "Console access" : "Development session";
});

watch(
  () => route.fullPath,
  () => {
    mobileNavigationOpen.value = false;
  },
);

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") mobileNavigationOpen.value = false;
}

async function signOut() {
  if (loggingOut.value) return;
  loggingOut.value = true;
  try {
    await logout();
  } finally {
    loggingOut.value = false;
  }
}

onMounted(async () => {
  window.addEventListener("keydown", onKeydown);
  initAgentBridge();
  connectWs();
  void refreshDemoStatus();
  try {
    await loadAuthState(true);
  } finally {
    sessionReady.value = true;
  }
});

onBeforeUnmount(() => {
  disconnectWs();
  if (typeof window !== "undefined") window.removeEventListener("keydown", onKeydown);
});
</script>

<template>
  <div v-if="isLoginRoute" class="min-h-dvh bg-[#FAFAFA] text-[#09090B]">
    <slot />
  </div>

  <div v-else class="min-h-dvh bg-[#FAFAFA] text-[#09090B]">
    <div class="min-h-dvh lg:grid lg:grid-cols-[15.25rem_minmax(0,1fr)]">
      <aside class="hidden border-r border-[#E4E4E7] bg-white lg:sticky lg:top-0 lg:flex lg:h-dvh lg:flex-col">
        <div class="flex h-16 items-center gap-3 border-b border-[#E4E4E7] px-5">
          <span class="flex h-8 w-8 items-center justify-center rounded border border-[#99F6E4] bg-[#F0FDFA] text-sm font-semibold text-[#0F766E]">M</span>
          <div class="min-w-0">
            <div class="truncate text-sm font-semibold tracking-tight">MCPcodeaway</div>
            <div class="text-[11px] text-[#71717A]">Admin control plane</div>
          </div>
        </div>

        <nav aria-label="Main navigation" class="min-h-0 flex-1 overflow-y-auto px-3 py-4">
          <div v-for="group in groupedNavigation" :key="group.name" class="mb-5 last:mb-0">
            <h2 class="mb-1.5 px-2 text-[11px] font-semibold text-[#71717A]">{{ group.name }}</h2>
            <div class="space-y-0.5">
              <NuxtLink
                v-for="item in group.items"
                :key="item.to"
                :to="item.to"
                class="group flex min-h-10 items-center gap-2.5 rounded-md px-2.5 py-2 text-[12px] font-medium no-underline transition-colors duration-150 hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
                :class="activeItem.to === item.to ? 'bg-[#F0FDFA] text-[#0F766E]' : 'text-[#52525B]'"
                :aria-current="activeItem.to === item.to ? 'page' : undefined"
              >
                <svg v-if="item.icon === 'overview'" aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 18a8 8 0 1 1 16 0" /><path d="m12 14 4-4M7 18h10" />
                </svg>
                <svg v-else-if="item.icon === 'analytics'" aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 6h16M4 12h16M4 18h16" /><circle cx="7" cy="6" r="1" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="1" fill="currentColor" stroke="none" /><circle cx="17" cy="18" r="1" fill="currentColor" stroke="none" />
                </svg>
                <svg v-else-if="item.icon === 'traces'" aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 6h5v5H4zM15 13h5v5h-5zM9 8h3a3 3 0 0 1 3 3v2M9 16h3" />
                </svg>
                <svg v-else-if="item.icon === 'models'" aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="m12 3 8 4.5v9L12 21l-8-4.5v-9zM12 3v18M4 7.5l8 4.5 8-4.5" />
                </svg>
                <svg v-else-if="item.icon === 'upstreams'" aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M7 7h10M7 17h10M5 5 3 7l2 2M19 15l2 2-2 2M12 7v10" />
                </svg>
                <svg v-else-if="item.icon === 'audit'" aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M6 3h9l4 4v14H6zM15 3v5h4M9 12h7M9 16h7" />
                </svg>
                <svg v-else-if="item.icon === 'onboarding'" aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M12 5v14M5 12h14" />
                </svg>
                <svg v-else-if="item.icon === 'settings'" aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-1.6 1.6-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5v.2h-2.3v-.2a1.7 1.7 0 0 0-1-1.5 1.7 1.7 0 0 0-1.9.3l-.1.1-1.6-1.6.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H5v-2.3h.2a1.7 1.7 0 0 0 1.5-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1 1.6-1.6.1.1a1.7 1.7 0 0 0 1.9.3 1.7 1.7 0 0 0 1-1.5V6h2.3v.2a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1 1.6 1.6-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.5 1h.2v2.3h-.2a1.7 1.7 0 0 0-1.5.8Z" />
                </svg>
                <svg v-else aria-hidden="true" viewBox="0 0 24 24" class="h-[18px] w-[18px] shrink-0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="m8 5 11 7-11 7z"/><path d="M4 4v16" />
                </svg>
                <span class="min-w-0 flex-1 truncate">{{ item.label }}</span>
                <span v-if="item.group === 'Demo'" class="rounded border border-[#E4E4E7] px-1 py-0.5 text-[9px] font-semibold text-[#71717A]">SANDBOX</span>
              </NuxtLink>
            </div>
          </div>
        </nav>

        <div class="border-t border-[#E4E4E7] p-3">
          <div class="flex items-center gap-2.5 rounded-md px-2 py-2">
            <span class="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-[#F4F4F5] text-xs font-semibold text-[#52525B]">{{ String(adminIdentity).slice(0, 1).toUpperCase() }}</span>
            <div class="min-w-0 flex-1">
              <div class="truncate text-xs font-semibold text-[#09090B]">{{ sessionReady ? adminIdentity : "Checking admin session…" }}</div>
              <div class="text-[11px] text-[#71717A]">{{ adminRole }}</div>
            </div>
            <button
              v-if="session?.authenticated"
              type="button"
              class="min-h-9 shrink-0 rounded-md border border-[#E4E4E7] px-2 text-[11px] font-medium text-[#52525B] hover:bg-[#F4F4F5] disabled:opacity-50"
              :disabled="loggingOut"
              @click="signOut"
            >{{ loggingOut ? "Signing out…" : "Sign out" }}</button>
          </div>
        </div>
      </aside>

      <div class="min-w-0">
        <header class="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-[#E4E4E7] bg-white px-4 sm:px-6 lg:px-8">
          <div class="flex min-w-0 items-center gap-3">
            <button
              type="button"
              class="inline-flex h-10 w-10 items-center justify-center rounded-md border border-[#E4E4E7] text-[#52525B] hover:bg-[#F4F4F5] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E] lg:hidden"
              aria-label="Open navigation"
              :aria-expanded="mobileNavigationOpen"
              @click="mobileNavigationOpen = true"
            >
              <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M4 6h16M4 12h16M4 18h16" /></svg>
            </button>
            <div class="min-w-0">
              <div class="truncate text-sm font-semibold text-[#09090B]">{{ activeItem.label }}</div>
              <div class="text-[11px] text-[#71717A]">MCPcodeaway Gateway</div>
            </div>
          </div>
          <div class="flex items-center gap-2 text-[11px] text-[#52525B]">
            <span class="hidden rounded border border-[#E4E4E7] bg-[#FAFAFA] px-2 py-1 font-medium lg:inline-flex">CONTROL PLANE</span>
            <label v-if="isReportPage" title="All time covers the telemetry currently retained by the gateway." class="inline-flex min-w-0 items-center gap-1.5 rounded-md border border-[#E4E4E7] bg-white px-2 py-1.5 text-xs text-[#52525B] sm:gap-2 sm:px-2.5">
              <span class="hidden sm:inline">Range</span>
              <select v-model="selectedRange" aria-label="Report time range" class="min-w-0 max-w-[7rem] bg-transparent font-medium text-[#09090B] outline-none sm:max-w-none sm:min-w-[7.5rem]">
                <option value="1h">Last hour</option>
                <option value="24h">Last 24 hours</option>
                <option value="7d">Last 7 days</option>
                <option value="30d">Last 30 days</option>
                <option value="all">All time</option>
              </select>
            </label>
            <span class="hidden items-center gap-1.5 rounded border border-[#E4E4E7] px-2 py-1 sm:inline-flex">
              <span class="h-1.5 w-1.5 rounded-full bg-[#0F766E]"></span>
              {{ adminRole }}
            </span>
          </div>
        </header>

        <main class="mx-auto w-full max-w-[1600px] p-4 sm:p-6 lg:p-8">
          <slot />
        </main>
      </div>
    </div>

    <div v-if="mobileNavigationOpen" class="fixed inset-0 z-50 lg:hidden" role="dialog" aria-modal="true" aria-label="Main navigation">
      <button class="absolute inset-0 h-full w-full bg-black/20" aria-label="Close navigation" @click="mobileNavigationOpen = false"></button>
      <aside class="relative flex h-full w-[min(19rem,88vw)] flex-col border-r border-[#E4E4E7] bg-white shadow-xl">
        <div class="flex h-16 items-center justify-between border-b border-[#E4E4E7] px-4">
          <div class="flex items-center gap-2.5">
            <span class="flex h-8 w-8 items-center justify-center rounded border border-[#99F6E4] bg-[#F0FDFA] text-sm font-semibold text-[#0F766E]">M</span>
            <span class="text-sm font-semibold">MCPcodeaway</span>
          </div>
          <button type="button" class="flex h-10 w-10 items-center justify-center rounded-md hover:bg-[#F4F4F5]" aria-label="Close navigation" @click="mobileNavigationOpen = false">
            <svg viewBox="0 0 24 24" class="h-5 w-5" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="m6 6 12 12M18 6 6 18" /></svg>
          </button>
        </div>
        <nav aria-label="Mobile navigation" class="min-h-0 flex-1 overflow-y-auto px-3 py-4">
          <div v-for="group in groupedNavigation" :key="group.name" class="mb-5 last:mb-0">
            <h2 class="mb-1.5 px-2 text-[11px] font-semibold text-[#71717A]">{{ group.name }}</h2>
            <div class="space-y-0.5">
              <NuxtLink
                v-for="item in group.items"
                :key="item.to"
                :to="item.to"
                class="flex min-h-11 items-center gap-2.5 rounded-md px-2.5 py-2 text-[12px] font-medium no-underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#0F766E]"
                :class="activeItem.to === item.to ? 'bg-[#F0FDFA] text-[#0F766E]' : 'text-[#52525B] hover:bg-[#F4F4F5]'"
              >
                <span class="min-w-0 flex-1 truncate">{{ item.label }}</span>
                <span v-if="item.group === 'Demo'" class="rounded border border-[#E4E4E7] px-1 py-0.5 text-[9px] font-semibold text-[#71717A]">SANDBOX</span>
              </NuxtLink>
            </div>
          </div>
        </nav>
        <div class="border-t border-[#E4E4E7] p-3 text-xs">
          <div class="break-all font-semibold text-[#09090B]">{{ adminIdentity }}</div>
          <div class="mt-0.5 text-[#71717A]">{{ adminRole }}</div>
          <button
            v-if="session?.authenticated"
            type="button"
            class="mt-3 min-h-11 w-full rounded-md border border-[#E4E4E7] px-3 text-left text-xs font-medium text-[#52525B] hover:bg-[#F4F4F5] disabled:opacity-50"
            :disabled="loggingOut"
            @click="signOut"
          >{{ loggingOut ? "Signing out…" : "Sign out" }}</button>
        </div>
      </aside>
    </div>
  </div>
</template>
