// Keep this entry independent of the app's module graph: a failed dependency
// download must leave a usable page, not an unhandled import and eternal splash.
const retryKey = "talaria-module-retry";
let failed = false;
let reloadTimer;
const timer = setTimeout(recover, 15000);

function recover() {
  if (failed) return;
  failed = true;
  clearTimeout(timer);
  const splash = document.querySelector("#app .initial-loader");
  if (!splash) return;
  splash.querySelector('[role="status"]').textContent = "Could not load Talaria.";
  splash.querySelector("a").classList.add("is-visible");
  // Failed imports remain cached for this document. Retry with a fresh one,
  // at most once until a module graph loads successfully; never loop offline.
  const retry = () => {
    if (!splash.isConnected) return;
    try {
      if (sessionStorage.getItem(retryKey)) return;
      sessionStorage.setItem(retryKey, "1");
      reloadTimer = setTimeout(() => location.reload(), 1000);
    } catch (_error) { /* The reload link also works without browser storage. */ }
  };
  if (navigator.onLine) retry();
  else window.addEventListener("online", retry, { once: true });
}

import("./app.js").then(() => {
  clearTimeout(timer);
  clearTimeout(reloadTimer);
  try { sessionStorage.removeItem(retryKey); } catch (_error) { /* Optional storage. */ }
}).catch(recover);
