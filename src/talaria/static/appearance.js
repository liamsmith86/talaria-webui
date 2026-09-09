(() => {
  window.Prism = { manual: true };
  try {
    const theme = localStorage.getItem("talaria.theme") || "system";
    const palette = localStorage.getItem("talaria.palette") || "blue";
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.palette = palette;
  } catch {
    /* Private browsing can disable storage. */
  }
})();
