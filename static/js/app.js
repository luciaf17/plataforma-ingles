// Mobile sidebar toggle. Vanilla JS on purpose: no framework in this project.
function toggleSide(open) {
  document.getElementById("side").classList.toggle("open", open);
  document.getElementById("scrim").classList.toggle("on", open);
}
