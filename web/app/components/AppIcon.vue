<script setup lang="ts">
export type IconName =
  | "menu"
  | "x"
  | "overview"
  | "analytics"
  | "traces"
  | "operations"
  | "models"
  | "business-value"
  | "workflows"
  | "onboarding"
  | "upstreams"
  | "audit"
  | "mcp-ui"
  | "settings"
  | "demo"
  | "logout"
  | "refresh"
  | "check"
  | "alert"
  | "cpu"
  | "shield"
  | "flask"
  | "copy"
  | "trash"
  | "chevron-down"
  | "chevron-right"
  | "lock";

withDefaults(
  defineProps<{
    name: IconName;
    size?: number;
  }>(),
  {
    size: 16,
  }
);
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.5"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    class="shrink-0"
  >
    <template v-if="name === 'menu'">
      <path d="M4 6h16M4 12h16M4 18h16" />
    </template>
    <template v-else-if="name === 'x'">
      <path d="M18 6 6 18M6 6l12 12" />
    </template>
    <template v-else-if="name === 'overview'">
      <path d="M4 18a8 8 0 1 1 16 0" />
      <path d="m12 14 4-4" />
      <path d="M7 18h10" />
    </template>
    <template v-else-if="name === 'analytics'">
      <path d="M3 20h18" />
      <rect x="5" y="11" width="3" height="9" rx=".5" />
      <rect x="10.5" y="6" width="3" height="14" rx=".5" />
      <rect x="16" y="9" width="3" height="11" rx=".5" />
    </template>
    <template v-else-if="name === 'traces'">
      <rect x="3" y="4" width="6" height="4" rx="1" />
      <rect x="9" y="10" width="8" height="4" rx="1" />
      <rect x="13" y="16" width="8" height="4" rx="1" />
      <path d="M6 8v4h3M11 14v4h2" />
    </template>
    <template v-else-if="name === 'operations'">
      <path d="M22 12h-4l-3 7-4-14-3 7H2" />
    </template>
    <template v-else-if="name === 'models'">
      <path d="m12 3 8 4.5v9L12 21l-8-4.5v-9z" />
      <path d="M12 12v9M4 7.5l8 4.5 8-4.5" />
    </template>
    <template v-else-if="name === 'business-value'">
      <path d="M3 17l6-6 4 4 8-8" />
      <path d="M16 7h5v5" />
    </template>
    <template v-else-if="name === 'workflows'">
      <circle cx="5" cy="12" r="2.5" />
      <circle cx="19" cy="6" r="2.5" />
      <circle cx="19" cy="18" r="2.5" />
      <path d="M7.5 12H11a3 3 0 0 0 3-3V6h2.5M11 12a3 3 0 0 1 3 3v3h2.5" />
    </template>
    <template v-else-if="name === 'onboarding'">
      <path d="M12 5v14M5 12h14" />
      <rect x="3" y="3" width="18" height="18" rx="2" />
    </template>
    <template v-else-if="name === 'upstreams'">
      <rect x="2" y="4" width="20" height="6" rx="1.5" />
      <rect x="2" y="14" width="20" height="6" rx="1.5" />
      <path d="M6 7h.01M6 17h.01" />
    </template>
    <template v-else-if="name === 'audit'">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6M8 13h8M8 17h5" />
    </template>
    <template v-else-if="name === 'mcp-ui'">
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M3 9h18M9 9v11" />
    </template>
    <template v-else-if="name === 'settings'">
      <circle cx="12" cy="12" r="3" />
      <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
    </template>
    <template v-else-if="name === 'demo'">
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="m10 8.5 6 3.5-6 3.5z" />
    </template>
    <template v-else-if="name === 'logout'">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="m16 17 5-5-5-5M21 12H9" />
    </template>
    <template v-else-if="name === 'refresh'">
      <path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
      <path d="M3 3v5h5" />
      <path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" />
      <path d="M16 16h5v5" />
    </template>
    <template v-else-if="name === 'check'">
      <path d="M20 6 9 17l-5-5" />
    </template>
    <template v-else-if="name === 'alert'">
      <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
      <path d="M12 9v4M12 17h.01" />
    </template>
    <template v-else-if="name === 'cpu'">
      <rect x="6" y="6" width="12" height="12" rx="1" />
      <path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4" />
    </template>
    <template v-else-if="name === 'shield'">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
      <path d="m9 12 2 2 4-4" />
    </template>
    <template v-else-if="name === 'flask'">
      <path d="M9 3h6M10 3v5l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3" />
      <path d="M6.5 14h11" />
    </template>
    <template v-else-if="name === 'copy'">
      <rect x="9" y="9" width="13" height="13" rx="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </template>
    <template v-else-if="name === 'trash'">
      <path d="M3 6h18M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2m2 0v14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V6h12z" />
    </template>
    <template v-else-if="name === 'chevron-down'">
      <path d="m6 9 6 6 6-6" />
    </template>
    <template v-else-if="name === 'chevron-right'">
      <path d="m9 6 6 6-6 6" />
    </template>
    <template v-else-if="name === 'lock'">
      <rect x="5" y="11" width="14" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </template>
  </svg>
</template>
