import tailwindcss from "@tailwindcss/vite";

export default defineNuxtConfig({
  compatibilityDate: "2025-07-15",
  telemetry: false,
  devtools: { enabled: false },
  ssr: false,
  modules: ["@ui-sdk/vue"],
  css: ["~/assets/css/main.css"],
  vite: {
    plugins: [tailwindcss()],
  },
  app: {
    baseURL: "/dashboard/",
    head: {
      title: "MCPcodeaway — Admin Control Plane",
      htmlAttrs: { lang: "en" },
      meta: [
        { name: "viewport", content: "width=device-width, initial-scale=1" },
        {
          name: "description",
          content:
            "MCPcodeaway Admin Control Plane — Runtime Telemetry, Analytics & A/B Onboarding",
        },
      ],
    },
  },
  runtimeConfig: {
    public: {
      apiBase: process.env.NUXT_PUBLIC_API_BASE || "",
    },
  },
  nitro: {
    devProxy: {
      "/health": "http://127.0.0.1:8000/health",
      "/ready": "http://127.0.0.1:8000/ready",
      "/auth": "http://127.0.0.1:8000/auth",
      "/admin": "http://127.0.0.1:8000/admin",
      "/audit": "http://127.0.0.1:8000/audit",
      "/metrics": "http://127.0.0.1:8000/metrics",
      "/compare": "http://127.0.0.1:8000/compare",
      "/benchmarks": "http://127.0.0.1:8000/benchmarks",
      "/mcp": "http://127.0.0.1:8000/mcp",
    },
  },
});
