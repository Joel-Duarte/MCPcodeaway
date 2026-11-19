<script setup lang="ts">
import type { IconName } from "~/components/AppIcon.vue";

const route = useRoute();
const { session, runtimeMeta, authConfig, logout } = useAdminApi();
const { pendingConfirm, webMcpActive, initAgentBridge, resolveConfirm } = useUiAgentBridge();
const {
  status: demoStatus,
  busy: demoBusy,
  refreshStatus: refreshDemoStatus,
  startTraffic,
  setTier,
  stopTraffic,
  runBurst,
} = useDemoTraffic();
const { connected: wsConnected, connect: connectWs, disconnect: disconnectWs } = useLiveTelemetry();

const navOpen = ref(false);

onMounted(async () => {
  initAgentBridge();
  await refreshDemoStatus();
  connectWs();
});

onBeforeUnmount(() => {
  disconnectWs();
});

watch(
  () => route.fullPath,
  () => {
    navOpen.value = false;
  }
);

const isLoginPage = computed(() => route.path === "/login");

const navItems: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Overview & Runtime", icon: "overview" },
  { to: "/analytics", label: "Analytics & Cost", icon: "analytics" },
  { to: "/onboarding", label: "Onboarding & A/B Lab", icon: "onboarding" },
  { to: "/upstreams", label: "Upstreams & Splits", icon: "upstreams" },
  { to: "/audit", label: "Audit & Retention", icon: "audit" },
];

function isActiveRoute(to: string): boolean {
  if (to === "/") {
    return route.path === "/" || route.path === "";
  }
  return route.path.startsWith(to);
}

const adminLabel = computed(() => {
  const user = session.value?.user;
  if (!user) return "Local Admin";
  return user.email || user.sub || "Admin";
});

const adminRoleText = computed(() => {
  const roles = session.value?.user?.roles;
  if (!roles || roles.length === 0) return "open";
  return roles[0];
});
</script>

<template>
  <div v-if="isLoginPage" class="min-h-dvh bg-[#FAFAFA] text-[#09090B]">
    <slot />
  </div>

  <div v-else class="min-h-dvh bg-[#FAFAFA] text-[#09090B] lg:flex">
    <!-- Persistent desktop navigation rail (240px) -->
    <aside
      aria-label="Primary navigation"
      class="hidden lg:flex lg:w-60 lg:shrink-0 lg:flex-col lg:border-r lg:border-[#E4E4E7] lg:bg-[#F4F4F5]"
    >
      <div class="flex h-14 items-center gap-2.5 border-b border-[#E4E4E7] px-4">
        <span
          class="inline-flex h-6 w-6 items-center justify-center rounded border border-[#99F6E4] bg-[#F0FDFA] text-xs font-semibold text-[#0F766E]"
        >
          M
        </span>
        <span class="text-sm font-semibold tracking-tight text-[#09090B]">
          MCPcodeaway
        </span>
      </div>

      <nav class="flex-1 space-y-1 p-3">
        <NuxtLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          class="no-underline flex items-center gap-2.5 rounded-md px-3 py-2 text-[13px] font-medium transition-colors"
          :class="
            isActiveRoute(item.to)
              ? 'bg-[#F0FDFA] text-[#0F766E] border border-[#99F6E4]'
              : 'text-[#52525B] border border-transparent hover:bg-[#E4E4E7]/60 hover:text-[#09090B]'
          "
        >
          <AppIcon :name="item.icon" :size="16" />
          <span>{{ item.label }}</span>
        </NuxtLink>
      </nav>

      <div class="border-t border-[#E4E4E7] p-3 text-xs text-[#52525B]">
        <div class="flex items-center justify-between gap-2">
          <span>Auth Mode</span>
          <span class="font-medium text-[#09090B]">
            {{ authConfig?.auth_mode || "—" }}
          </span>
        </div>
        <div class="mt-1.5 flex items-center justify-between gap-2">
          <span>UI Agent Bridge</span>
          <span class="font-medium text-[#0F766E]">
            {{ webMcpActive ? "WebMCP Ready" : "UI-SDK Ready" }}
          </span>
        </div>
      </div>
    </aside>

    <!-- Main column -->
    <div class="flex min-w-0 flex-1 flex-col">
      <!-- Top utility bar (56px) -->
      <header
        class="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-[#E4E4E7] bg-white px-4 lg:px-6"
      >
        <div class="flex min-w-0 items-center gap-2.5">
          <!-- Mobile drawer trigger (< lg) -->
          <div class="lg:hidden">
            <Drawer v-model:open="navOpen" side="left">
              <DrawerTrigger
                aria-label="Open navigation menu"
                class="min-h-11 min-w-11 px-2.5 text-[#09090B]"
              >
                <AppIcon name="menu" :size="18" />
              </DrawerTrigger>
              <DrawerBackdrop />
              <DrawerContent class="w-72 max-w-[85vw] bg-[#F4F4F5] p-0">
                <div class="flex h-14 items-center justify-between border-b border-[#E4E4E7] px-4">
                  <DrawerTitle class="text-sm font-semibold text-[#09090B]">
                    MCPcodeaway
                  </DrawerTitle>
                  <DrawerClose aria-label="Close navigation menu" class="min-h-10 px-2">
                    <AppIcon name="x" :size="16" />
                  </DrawerClose>
                </div>
                <DrawerDescription class="sr-only">
                  Admin console navigation links
                </DrawerDescription>
                <nav class="flex-1 space-y-1 p-3" aria-label="Mobile navigation">
                  <NuxtLink
                    v-for="item in navItems"
                    :key="item.to"
                    :to="item.to"
                    class="no-underline flex min-h-11 items-center gap-2.5 rounded-md px-3 py-2.5 text-sm font-medium transition-colors"
                    :class="
                      isActiveRoute(item.to)
                        ? 'bg-[#F0FDFA] text-[#0F766E] border border-[#99F6E4]'
                        : 'text-[#52525B] border border-transparent hover:bg-[#E4E4E7]/60 hover:text-[#09090B]'
                    "
                  >
                    <AppIcon :name="item.icon" :size="18" />
                    <span>{{ item.label }}</span>
                  </NuxtLink>
                </nav>
                <div class="border-t border-[#E4E4E7] p-4 text-xs text-[#52525B]">
                  <div>Profile: <span class="font-medium text-[#09090B]">{{ runtimeMeta.profile }}</span></div>
                  <div class="mt-1">Catalog: <span class="font-mono-data text-[#09090B]">{{ runtimeMeta.catalog_version }}</span></div>
                </div>
              </DrawerContent>
            </Drawer>
          </div>

          <span class="truncate text-sm font-semibold text-[#09090B] lg:hidden">
            MCPcodeaway
          </span>

          <div class="hidden sm:flex items-center gap-2 text-xs text-[#52525B]">
            <span class="rounded border border-[#E4E4E7] bg-[#F4F4F5] px-2 py-0.5 font-medium text-[#09090B]">
              profile: {{ runtimeMeta.profile }}
            </span>
            <span class="rounded border border-[#E4E4E7] bg-[#F4F4F5] px-2 py-0.5 font-medium text-[#09090B]">
              env: {{ runtimeMeta.app_env }}
            </span>
            <span class="hidden md:inline rounded border border-[#E4E4E7] bg-white px-2 py-0.5 font-mono-data text-[#52525B]">
              {{ runtimeMeta.catalog_version }}
            </span>
          </div>
        </div>

        <div class="flex shrink-0 items-center gap-2">
          <span
            class="hidden sm:inline-flex items-center gap-1.5 rounded border border-[#E4E4E7] bg-white px-2 py-0.5 text-[11px] font-medium text-[#52525B]"
            :title="wsConnected ? 'WebSocket telemetry connected' : 'WebSocket reconnecting'"
          >
            <span
              class="inline-block h-2 w-2 rounded-full"
              :class="wsConnected ? 'bg-[#059669]' : 'bg-[#D97706]'"
            />
            <span>{{ wsConnected ? "Live WS" : "WS Offline" }}</span>
          </span>
          <div
            v-if="demoStatus.enabled"
            class="hidden md:flex items-center gap-1.5 rounded-md border border-[#E4E4E7] bg-[#F4F4F5] px-2 py-1 text-xs"
          >
            <span
              class="inline-block h-2 w-2 rounded-full"
              :class="demoStatus.running ? 'bg-[#059669] animate-pulse' : 'bg-[#71717A]'"
            />
            <span class="font-medium text-[#09090B] tabular-nums">
              Demo {{ demoStatus.requests_sent }} reqs
            </span>
            <select
              aria-label="Demo traffic tier"
              :value="demoStatus.tier"
              class="rounded border border-[#D4D4D8] bg-white px-1.5 py-0.5 text-[11px] font-medium text-[#09090B]"
              @change="(e) => setTier((e.target as HTMLSelectElement).value as 'light' | 'enterprise' | 'stress')"
            >
              <option value="light">Light (~0.5 rps)</option>
              <option value="enterprise">Enterprise (~2 rps)</option>
              <option value="stress">Stress (~5 rps)</option>
            </select>
            <Button
              v-if="!demoStatus.running"
              class="px-2 py-0.5 text-[11px]"
              :disabled="demoBusy"
              @click="startTraffic()"
            >
              Start Demo Traffic
            </Button>
            <Button
              v-else
              data-variant="danger"
              class="px-2 py-0.5 text-[11px]"
              :disabled="demoBusy"
              @click="stopTraffic"
            >
              Stop Demo Traffic
            </Button>
            <Button
              class="px-2 py-0.5 text-[11px]"
              :disabled="demoBusy"
              @click="runBurst(8)"
            >
              Burst +8
            </Button>
          </div>
          <div class="hidden sm:flex items-center gap-2 text-xs">
            <span class="font-medium text-[#09090B] truncate max-w-[160px]">
              {{ adminLabel }}
            </span>
            <span class="rounded border border-[#99F6E4] bg-[#F0FDFA] px-1.5 py-0.5 text-[11px] font-medium text-[#0F766E]">
              {{ adminRoleText }}
            </span>
          </div>
          <Button
            v-if="session?.authenticated"
            data-variant="ghost"
            class="text-xs"
            @click="logout"
          >
            <AppIcon name="logout" :size="15" />
            <span>Sign out</span>
          </Button>
          <NuxtLink
            v-else-if="session?.admin_auth_required"
            to="/login"
            class="no-underline inline-flex items-center gap-1.5 rounded-md border border-[#0F766E] bg-[#0F766E] px-3 py-1.5 text-xs font-medium text-white hover:bg-[#115E59]"
          >
            <AppIcon name="lock" :size="14" />
            <span>Sign in</span>
          </NuxtLink>
        </div>
      </header>

      <!-- Workspace content -->
      <main class="mx-auto w-full max-w-7xl flex-1 p-4 sm:p-6">
        <slot />
      </main>
    </div>

    <!-- Human-in-the-loop confirmation dialog for data-agent-confirm actions -->
    <Dialog :open="Boolean(pendingConfirm)" @update:open="(v) => { if (!v) resolveConfirm(false); }">
      <DialogBackdrop />
      <DialogContent class="sm:max-w-md w-[calc(100%-2rem)]">
        <DialogTitle>Confirm UI Agent Action</DialogTitle>
        <DialogDescription class="mt-2 text-sm text-[#52525B]">
          {{ pendingConfirm?.message || "Approve this action requested by an agent?" }}
        </DialogDescription>
        <div class="mt-5 flex justify-end gap-2">
          <Button @click="resolveConfirm(false)">Deny Action</Button>
          <Button data-variant="primary" @click="resolveConfirm(true)">Approve Action</Button>
        </div>
      </DialogContent>
    </Dialog>
  </div>
</template>
