// Mobile sidebar toggle. Vanilla JS on purpose: no framework in this project.
function toggleSide(open) {
  document.getElementById("side").classList.toggle("open", open);
  document.getElementById("scrim").classList.toggle("on", open);
}

// Installable app: the service worker is served from the root so it covers every page.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch((err) => console.warn("Service worker not registered", err));
}
