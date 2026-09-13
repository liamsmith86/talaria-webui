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
  const scheme = matchMedia("(prefers-color-scheme: dark)");
  const color = document.querySelector('meta[name="theme-color"]');
  function updateChrome() {
    const theme = document.documentElement.dataset.theme;
    const dark = theme === "dark" || (theme !== "light" && scheme.matches);
    if (color) color.content = dark ? "#191c1b" : "#fbfbf9";
  }
  updateChrome();
  scheme.addEventListener("change", updateChrome);
  new MutationObserver(updateChrome).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
})();
