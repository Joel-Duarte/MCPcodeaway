<script setup lang="ts">
import { onMounted } from "vue";
import BusinessValueReportSection from "~/components/BusinessValueReportSection.vue";

onMounted(() => {
  if (typeof navigateTo === "function") {
    void navigateTo("/workflows?tab=ledger", { replace: true });
  }
});
</script>

<template>
  <div class="space-y-6">
    <header class="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 class="text-[20px] font-semibold tracking-[-0.015em] text-[#09090B]">Business Value</h1>
        <p class="mt-1 max-w-3xl text-[13px] leading-5 text-[#52525B]">
          See which administrator-defined workflows completed, what human baseline they measure, and which value estimates are supported by evidence.
        </p>
      </div>
      <NuxtLink
        to="/workflows?tab=ledger"
        class="inline-flex min-h-9 items-center rounded-md border border-[#99F6E4] bg-[#F0FDFA] px-3.5 text-xs font-medium text-[#0F766E] hover:bg-[#CCFBF1]"
      >
        Open unified Workflows &amp; ROI →
      </NuxtLink>
    </header>

    <BusinessValueReportSection />
  </div>
</template>
