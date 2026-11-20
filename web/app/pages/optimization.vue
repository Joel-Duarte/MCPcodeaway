<script setup lang="ts">
const route = useRoute();
await navigateTo({ path: "/model-performance", query: { ...route.query, tab: "efficiency" } }, { redirectCode: 302 });
</script>

<template><p class="text-sm text-[#52525B]">Opening efficiency report…</p></template>
