const root = document.documentElement;
let savedTheme;
try { savedTheme = localStorage.getItem("wildtrack-theme"); } catch (_) {}
if (savedTheme === "dark" || (!savedTheme && window.matchMedia("(prefers-color-scheme: dark)").matches)) {
  root.dataset.theme = "dark";
}

document.querySelector(".theme-toggle")?.addEventListener("click", () => {
  const nextTheme = root.dataset.theme === "dark" ? "light" : "dark";
  root.dataset.theme = nextTheme;
  try { localStorage.setItem("wildtrack-theme", nextTheme); } catch (_) {}
});

const menuButton = document.querySelector(".menu-toggle");
const menu = document.querySelector(".nav-menu");
menuButton?.addEventListener("click", () => {
  const isOpen = menu.classList.toggle("open");
  menuButton.setAttribute("aria-expanded", String(isOpen));
});

document.querySelectorAll(".flash button").forEach((button) => {
  button.addEventListener("click", () => button.closest(".flash")?.remove());
});
