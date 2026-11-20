<script setup lang="ts">
import { extractErrorMessage } from "~/composables/useAdminApi";

const router = useRouter();
const route = useRoute();
const {
  authConfig,
  session,
  loadAuthState,
  devLogin,
  tokenLogin,
  oidcLoginUrl,
} = useAdminApi();

const selectedPersona = ref<string>("gateway_admin");
const bearerInput = ref("");
const errorMessage = ref("");
const initialLoading = ref(true);
const accessDenied = computed(() => route.query.auth_error === "not_authorized");

onMounted(async () => {
  try {
    const { authConfig: cfg, session: sess } = await loadAuthState(true);
    if (cfg.dev_personas.length > 0 && !cfg.dev_personas.includes(selectedPersona.value)) {
      selectedPersona.value = cfg.dev_personas[0] || "gateway_admin";
    }
    if (!sess.admin_auth_required || (sess.authenticated && sess.can_access_admin_console)) {
      await router.replace("/");
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  } finally {
    initialLoading.value = false;
  }
});

async function handleDevSignIn() {
  errorMessage.value = "";
  try {
    const res = await devLogin(selectedPersona.value);
    if (res.authenticated && res.can_access_admin_console) {
      await router.push("/");
    } else {
      errorMessage.value = "Selected persona lacks an admin or viewer role.";
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

async function handleTokenSignIn() {
  errorMessage.value = "";
  if (!bearerInput.value.trim()) {
    errorMessage.value = "Paste an admin Bearer JWT before submitting.";
    return;
  }
  try {
    const res = await tokenLogin(bearerInput.value);
    bearerInput.value = "";
    if (res.authenticated && res.can_access_admin_console) {
      await router.push("/");
    } else {
      errorMessage.value = "Token lacks a gateway admin or viewer role.";
    }
  } catch (err) {
    errorMessage.value = extractErrorMessage(err);
  }
}

const personaItems = computed(() => {
  const list = authConfig.value?.dev_personas || ["gateway_admin"];
  return list.map((p) => ({ value: p, label: p }));
});
</script>

<template>
  <div class="flex min-h-dvh items-center justify-center bg-[#FAFAFA] p-4">
    <div class="w-full max-w-md rounded-lg border border-[#E4E4E7] bg-white p-6">
      <div class="flex items-center gap-2.5">
        <span
          class="inline-flex h-7 w-7 items-center justify-center rounded border border-[#99F6E4] bg-[#F0FDFA] text-sm font-semibold text-[#0F766E]"
        >
          M
        </span>
        <div>
          <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">
            MCPcodeaway Admin Console
          </h1>
          <p class="text-xs text-[#52525B]">
            Control-plane sign in for runtime telemetry, analytics &amp; onboarding
          </p>
        </div>
      </div>

      <Separator class="my-5" />

      <div v-if="initialLoading" class="space-y-3" aria-busy="true">
        <div class="h-10 w-full animate-pulse rounded-md bg-[#F4F4F5]" />
        <div class="h-10 w-full animate-pulse rounded-md bg-[#F4F4F5]" />
      </div>

      <div v-else class="space-y-5">
        <div
          v-if="authConfig"
          class="flex flex-wrap items-center gap-2 rounded-md border border-[#E4E4E7] bg-[#F4F4F5] px-3 py-2 text-xs text-[#52525B]"
        >
          <span>Profile: <strong class="text-[#09090B]">{{ authConfig.profile }}</strong></span>
          <span>Auth mode: <strong class="text-[#09090B]">{{ authConfig.auth_mode }}</strong></span>
        </div>

        <div
          v-if="errorMessage"
          role="alert"
          class="flex items-start gap-2 rounded-md border border-[#FECACA] bg-[#FEF2F2] px-3 py-2.5 text-[13px] text-[#DC2626]"
        >
          <AppIcon name="alert" :size="16" class="mt-0.5" />
          <span>{{ errorMessage }}</span>
        </div>

        <div
          v-if="accessDenied"
          role="alert"
          class="rounded-md border border-[#FDE68A] bg-[#FFFBEB] px-3 py-2.5 text-[13px] text-[#92400E]"
        >
          Your company account signed in, but it is not assigned access to this console. Ask your company administrator to grant the appropriate gateway role, then try again.
        </div>

        <!-- OIDC Authorization Code + PKCE -->
        <div class="space-y-2">
          <Button
            v-if="authConfig?.oidc_enabled"
            data-variant="primary"
            class="w-full"
          >
            <template #render="{ props }">
              <a v-bind="props" :href="oidcLoginUrl()" class="no-underline w-full">
                <AppIcon name="shield" :size="16" />
                <span>Sign in with company SSO</span>
              </a>
            </template>
          </Button>
          <p v-else class="text-xs text-[#71717A]">
            Company SSO is not configured for this deployment. Contact your gateway administrator.
          </p>
        </div>

        <!-- Dev Admin Persona -->
        <div v-if="authConfig?.dev_auth_enabled" class="space-y-3 border-t border-[#E4E4E7] pt-4">
          <h2 class="text-[15px] font-semibold text-[#09090B]">
            Dev Admin Persona
          </h2>
          <Field>
            <FieldLabel>Persona</FieldLabel>
            <Select
              :items="personaItems"
              :value="selectedPersona"
              @value-change="(val) => { if (val) selectedPersona = String(val) }"
            >
              <SelectTrigger>
                <SelectValue />
                <AppIcon name="chevron-down" :size="15" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem
                  v-for="item in personaItems"
                  :key="item.value"
                  :value="item.value"
                >
                  {{ item.label }}
                  <SelectItemIndicator>
                    <AppIcon name="check" :size="14" />
                  </SelectItemIndicator>
                </SelectItem>
              </SelectContent>
            </Select>
            <FieldDescription>
              Select `gateway_admin` for full control-plane access (other personas test 403 RBAC denial).
            </FieldDescription>
          </Field>
          <Button
            data-variant="primary"
            class="w-full"
            pending-announcement="Signing in"
            @click="handleDevSignIn"
          >
            Sign in as {{ selectedPersona }}
          </Button>
        </div>

        <!-- Bearer Token Exchange -->
        <form
          v-if="authConfig?.token_login_enabled"
          class="space-y-3 border-t border-[#E4E4E7] pt-4"
          @submit.prevent="handleTokenSignIn"
        >
          <h2 class="text-[15px] font-semibold text-[#09090B]">
            Development token exchange
          </h2>
          <Field>
            <FieldLabel>Bearer Token</FieldLabel>
            <Input
              v-model="bearerInput"
              type="password"
              autocomplete="off"
              placeholder="eyJhbGciOiJSUzI1NiIs..."
              class="font-mono-data text-xs"
            />
            <FieldDescription>
              Exchanges an admin JWT for an HttpOnly HMAC-signed admin cookie.
            </FieldDescription>
          </Field>
          <Button type="submit" class="w-full">
            Exchange Token &amp; Sign In
          </Button>
        </form>

        <div
          v-if="session && !session.admin_auth_required"
          class="text-center text-xs text-[#52525B]"
        >
          Admin auth is open in this environment.
          <NuxtLink to="/" class="text-[#0F766E] hover:underline">Go to Overview</NuxtLink>
        </div>
      </div>
    </div>
  </div>
</template>
