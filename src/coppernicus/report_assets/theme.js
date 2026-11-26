// The page's theme: dark (data-theme on the html element) unless the reader picks light with
// the button in the header. The choice is kept in localStorage where the browser allows it;
// some block it on file:// pages, and then the choice lasts while the page is open. Runs in the
// head, so a kept choice applies before the page is drawn.
(() => {
  "use strict";
  const KEY = "coppernicus-theme", root = document.documentElement;
  function load() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function save(theme) {
    try { window.localStorage.setItem(KEY, theme); } catch (e) { /* not kept: this page only */ }
  }
  const current = () => (root.getAttribute("data-theme") === "light" ? "light" : "dark");
  function label(button) {             // the button names the theme it switches to
    const next = current() === "dark" ? "light" : "dark";
    button.textContent = next === "light" ? "☀ Light" : "☾ Dark";
    button.setAttribute("aria-label", `Switch to the ${next} theme`);
    button.setAttribute("title", `Switch to the ${next} theme`);
  }
  const kept = load();
  if (kept === "light" || kept === "dark") root.setAttribute("data-theme", kept);
  function wire() {
    const button = document.getElementById("theme-toggle");
    if (!button) return;
    label(button);
    button.addEventListener("click", () => {
      const theme = current() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", theme);
      label(button);
      save(theme);
      document.dispatchEvent(new Event("coppernicus-theme"));     // the map redraws its outline
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
