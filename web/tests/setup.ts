import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { vi } from "vitest";

declare global {
  var __UI_SDK_STRICT__: boolean | undefined;
}
globalThis.__UI_SDK_STRICT__ = true;

if (typeof window !== "undefined") {
  window.matchMedia ||= (() =>
    ({
      matches: false,
      media: "",
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }) as MediaQueryList);

  if (!globalThis.ResizeObserver) {
    globalThis.ResizeObserver = class ResizeObserver {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
  if (!Element.prototype.scrollIntoView) {
    Element.prototype.scrollIntoView = () => {};
  }
}

const stateStore = new Map<string, ReturnType<typeof ref>>();

vi.stubGlobal("ref", ref);
vi.stubGlobal("computed", computed);
vi.stubGlobal("watch", watch);
vi.stubGlobal("onMounted", onMounted);
vi.stubGlobal("onBeforeUnmount", onBeforeUnmount);
vi.stubGlobal("useRuntimeConfig", () => ({ public: { apiBase: "" } }));
vi.stubGlobal("useRoute", () => ({ path: "/onboarding", fullPath: "/onboarding" }));
vi.stubGlobal("useRouter", () => ({ push: vi.fn() }));
vi.stubGlobal("useState", <T>(key: string, init?: () => T) => {
  if (!stateStore.has(key)) {
    stateStore.set(key, ref(init ? init() : undefined));
  }
  return stateStore.get(key)!;
});
vi.stubGlobal(
  "$fetch",
  vi.fn(async (url: string) => {
    if (url.includes("/admin/upstreams/agent-prompt")) {
      return { prompt: "Test prompt with POST /admin/mcp and WebMCP", schema: {} };
    }
    if (url.includes("/admin/upstreams/inspect")) {
      return {
        inspection: { upstream: "demo_api", type: "openapi", tool_count: 1, tools: [] },
        onboarding_audit: { summary: "1 tool ok", warnings: [] },
      };
    }
    if (url.includes("/admin/upstreams")) {
      return { dynamic_upstreams_permitted: true, upstreams: [] };
    }
    return {};
  })
);
