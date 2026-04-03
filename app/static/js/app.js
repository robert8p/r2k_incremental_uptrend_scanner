const AUTH_TOKEN_KEY = "r2k_auth_token";
const ADMIN_PASSWORD_KEY = "r2k_admin_password";

function withToken(urlString, token) {
  if (!token) return urlString;
  try {
    const url = new URL(urlString, window.location.origin);
    url.searchParams.set("token", token);
    return url.pathname + url.search + url.hash;
  } catch {
    return urlString;
  }
}

function applyStoredAccess() {
  const authToken = localStorage.getItem(AUTH_TOKEN_KEY) || "";
  const authInput = document.getElementById("auth-token-input");
  const adminInput = document.getElementById("admin-password-input");
  if (authInput) authInput.value = authToken;
  if (adminInput) adminInput.value = localStorage.getItem(ADMIN_PASSWORD_KEY) || "";

  document.querySelectorAll("a[href]").forEach((anchor) => {
    const href = anchor.getAttribute("href") || "";
    if (!href || href.startsWith("http") || href.startsWith("mailto:") || href.startsWith("#")) return;
    anchor.setAttribute("href", withToken(href, authToken));
  });

  document.querySelectorAll("form[action]").forEach((form) => {
    const action = form.getAttribute("action") || "";
    form.setAttribute("action", withToken(action, authToken));
  });
}

function initAccessBar() {
  const saveButton = document.getElementById("access-save-button");
  const clearButton = document.getElementById("access-clear-button");
  const authInput = document.getElementById("auth-token-input");
  const adminInput = document.getElementById("admin-password-input");
  if (!saveButton || !clearButton || !authInput || !adminInput) return;

  saveButton.addEventListener("click", () => {
    localStorage.setItem(AUTH_TOKEN_KEY, authInput.value.trim());
    localStorage.setItem(ADMIN_PASSWORD_KEY, adminInput.value);
    applyStoredAccess();
  });

  clearButton.addEventListener("click", () => {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    localStorage.removeItem(ADMIN_PASSWORD_KEY);
    authInput.value = "";
    adminInput.value = "";
    applyStoredAccess();
  });

  const currentToken = new URLSearchParams(window.location.search).get("token");
  if (currentToken && !localStorage.getItem(AUTH_TOKEN_KEY)) {
    localStorage.setItem(AUTH_TOKEN_KEY, currentToken);
  }
}

function initSortableTables() {
  document.querySelectorAll("table.sortable").forEach((table) => {
    table.querySelectorAll("th[data-sort]").forEach((th, index) => {
      let direction = 1;
      th.addEventListener("click", () => {
        const type = th.dataset.sort;
        const tbody = table.querySelector("tbody");
        const rows = Array.from(tbody.querySelectorAll("tr"));
        rows.sort((a, b) => {
          const aText = a.children[index]?.innerText?.trim() ?? "";
          const bText = b.children[index]?.innerText?.trim() ?? "";
          if (type === "number") {
            const aNum = parseFloat(aText.replace(/[^0-9.\-]/g, "")) || 0;
            const bNum = parseFloat(bText.replace(/[^0-9.\-]/g, "")) || 0;
            return direction * (aNum - bNum);
          }
          return direction * aText.localeCompare(bText);
        });
        tbody.innerHTML = "";
        rows.forEach((row) => tbody.appendChild(row));
        direction *= -1;
      });
    });
  });
}

function initTableSearch() {
  document.querySelectorAll(".table-search").forEach((input) => {
    input.addEventListener("input", () => {
      const tableId = input.dataset.targetTable;
      const table = document.getElementById(tableId);
      if (!table) return;
      const needle = input.value.trim().toLowerCase();
      table.querySelectorAll("tbody tr").forEach((row) => {
        row.style.display = row.innerText.toLowerCase().includes(needle) ? "" : "none";
      });
    });
  });
}

document.addEventListener("DOMContentLoaded", () => {
  initAccessBar();
  applyStoredAccess();
  initSortableTables();
  initTableSearch();
});
