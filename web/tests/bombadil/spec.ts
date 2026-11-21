import { actions, always, extract } from "@antithesishq/bombadil";

export * from "@antithesishq/bombadil/browser/defaults";

export const waitForHydration = actions(() => ["Wait"] as const);

const domMetrics = extract((state) => {
  const doc = state.document;
  const textLen = doc.body?.innerText?.trim().length ?? 0;
  const overflowPx = doc.documentElement.scrollWidth - doc.documentElement.clientWidth;
  const hasHeading = Boolean(doc.querySelector("h1, h2, [role=heading]"));
  return { textLen, overflowPx, hasHeading };
});

export const noHorizontalOverflow = always(() => {
  const m = domMetrics.current;
  return !m || m.overflowPx <= 2;
});

export const noBlankCrashScreen = always(() => {
  const m = domMetrics.current;
  if (!m || m.textLen === 0) return true;
  return m.textLen > 20 && m.hasHeading;
});
