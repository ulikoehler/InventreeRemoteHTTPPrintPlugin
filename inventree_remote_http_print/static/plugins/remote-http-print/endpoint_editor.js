/**
 * Custom admin settings UI for the Remote HTTP print service plugin.
 *
 * Provides a graphical editor for the ENDPOINTS setting (a JSON list of
 * {name, url} objects) and a dropdown for DEFAULT_ENDPOINT.
 *
 * The function signature is (container, context) — "legacy mode" — which
 * allows plain DOM manipulation without requiring a React build pipeline.
 */
export function renderPluginSettings(container, context) {
  const ctx = context.context || {};
  const slug = ctx.slug || "remote-http-print";
  const settings = ctx.settings || {};

  // -- Hide native form fields for ENDPOINTS and DEFAULT_ENDPOINT ---------
  // The graphical editor below replaces these; hide the raw JSON / text
  // inputs that InvenTree renders automatically from the SETTINGS dict.
  function hideNativeFields() {
    // InvenTree PUI renders settings as elements with data-setting-key
    const selectors = [
      '[data-setting-key="ENDPOINTS"]',
      '[data-setting-key="DEFAULT_ENDPOINT"]',
    ];
    selectors.forEach((sel) => {
      document.querySelectorAll(sel).forEach((el) => {
        el.style.display = "none";
      });
    });

    // Fallback: also hide by looking for labels containing the setting keys
    // (covers older InvenTree versions or different rendering paths)
    const labels = document.querySelectorAll("label, .setting-label, .label");
    labels.forEach((label) => {
      const text = (label.textContent || "").trim().toUpperCase();
      if (text === "ENDPOINTS" || text === "DEFAULT_ENDPOINT" ||
          text === "DEFAULT ENDPOINT") {
        // Walk up to find the container row/wrapper
        let row = label.closest("tr, .form-group, .setting-item, .paper, .card, div");
        if (row && row !== container) {
          row.style.display = "none";
        }
      }
    });
  }

  hideNativeFields();
  // Re-run after a delay in case the settings are rendered asynchronously
  setTimeout(hideNativeFields, 500);
  setTimeout(hideNativeFields, 1500);

  // Parse current endpoints
  let endpoints = [];
  try {
    endpoints = JSON.parse(settings.ENDPOINTS || "[]");
    if (!Array.isArray(endpoints)) endpoints = [];
  } catch (e) {
    endpoints = [];
  }

  let defaultEndpoint = settings.DEFAULT_ENDPOINT || "";

  // -- Styles -----------------------------------------------------------
  const style = document.createElement("style");
  style.textContent = `
    .ep-editor { font-family: inherit; max-width: 700px; }
    .ep-editor table { width: 100%; border-collapse: collapse; margin-bottom: 12px; }
    .ep-editor th { text-align: left; padding: 6px 8px; font-size: 13px; color: #666; border-bottom: 1px solid #ddd; }
    .ep-editor td { padding: 4px 8px; border-bottom: 1px solid #eee; }
    .ep-editor input[type="text"] { width: 100%; padding: 4px 6px; border: 1px solid #ccc; border-radius: 3px; font-size: 13px; }
    .ep-editor .ep-actions { white-space: nowrap; }
    .ep-editor button { cursor: pointer; border: none; border-radius: 3px; padding: 4px 10px; font-size: 13px; }
    .ep-editor .ep-add { background: #2196f3; color: #fff; margin-bottom: 12px; }
    .ep-editor .ep-del { background: #f44336; color: #fff; padding: 2px 8px; }
    .ep-editor .ep-save { background: #4caf50; color: #fff; margin-top: 8px; }
    .ep-editor .ep-msg { margin-top: 8px; font-size: 13px; }
    .ep-editor .ep-msg.ok { color: #4caf50; }
    .ep-editor .ep-msg.err { color: #f44336; }
    .ep-editor .ep-default-row { margin-top: 16px; }
    .ep-editor select { padding: 4px 6px; border: 1px solid #ccc; border-radius: 3px; font-size: 13px; }
    .ep-editor label { font-size: 14px; font-weight: 600; display: block; margin-bottom: 4px; }
  `;
  container.appendChild(style);

  const wrapper = document.createElement("div");
  wrapper.className = "ep-editor";
  container.appendChild(wrapper);

  // -- Title ------------------------------------------------------------
  const title = document.createElement("h3");
  title.textContent = "Print Endpoints";
  wrapper.appendChild(title);

  const subtitle = document.createElement("p");
  subtitle.style.fontSize = "13px";
  subtitle.style.color = "#666";
  subtitle.textContent =
    "Configure one or more BrotherQL print service endpoints. " +
    "Each endpoint has a name (shown in the print dialog) and a URL.";
  wrapper.appendChild(subtitle);

  // -- Table ------------------------------------------------------------
  const table = document.createElement("table");
  table.appendChild(
    (() => {
      const thead = document.createElement("thead");
      thead.innerHTML =
        "<tr><th>Name</th><th>URL</th><th></th></tr>";
      return thead;
    })()
  );
  const tbody = document.createElement("tbody");
  table.appendChild(tbody);
  wrapper.appendChild(table);

  function renderRows() {
    tbody.innerHTML = "";
    endpoints.forEach((ep, i) => {
      const tr = document.createElement("tr");

      const tdName = document.createElement("td");
      const nameInput = document.createElement("input");
      nameInput.type = "text";
      nameInput.value = ep.name || "";
      nameInput.placeholder = "e.g. Office";
      nameInput.addEventListener("change", () => {
        endpoints[i].name = nameInput.value;
      });
      tdName.appendChild(nameInput);
      tr.appendChild(tdName);

      const tdUrl = document.createElement("td");
      const urlInput = document.createElement("input");
      urlInput.type = "text";
      urlInput.value = ep.url || "";
      urlInput.placeholder = "http://printer.local:8080";
      urlInput.addEventListener("change", () => {
        endpoints[i].url = urlInput.value;
      });
      tdUrl.appendChild(urlInput);
      tr.appendChild(tdUrl);

      const tdActions = document.createElement("td");
      tdActions.className = "ep-actions";
      const delBtn = document.createElement("button");
      delBtn.className = "ep-del";
      delBtn.textContent = "Remove";
      delBtn.addEventListener("click", () => {
        endpoints.splice(i, 1);
        renderRows();
        renderDefaultDropdown();
      });
      tdActions.appendChild(delBtn);
      tr.appendChild(tdActions);

      tbody.appendChild(tr);
    });
  }

  // -- Add button -------------------------------------------------------
  const addBtn = document.createElement("button");
  addBtn.className = "ep-add";
  addBtn.textContent = "+ Add endpoint";
  addBtn.addEventListener("click", () => {
    endpoints.push({ name: "", url: "" });
    renderRows();
    renderDefaultDropdown();
  });
  wrapper.appendChild(addBtn);

  // -- Default endpoint dropdown ---------------------------------------
  const defaultRow = document.createElement("div");
  defaultRow.className = "ep-default-row";
  wrapper.appendChild(defaultRow);

  const defaultLabel = document.createElement("label");
  defaultLabel.textContent = "Default endpoint";
  defaultRow.appendChild(defaultLabel);

  const defaultSelect = document.createElement("select");
  defaultRow.appendChild(defaultSelect);

  const defaultHint = document.createElement("p");
  defaultHint.style.fontSize = "12px";
  defaultHint.style.color = "#999";
  defaultHint.textContent =
    "Used when no endpoint is selected in the print dialog. " +
    "Leave blank to use the first endpoint.";
  defaultRow.appendChild(defaultHint);

  function renderDefaultDropdown() {
    defaultSelect.innerHTML = "";
    const emptyOpt = document.createElement("option");
    emptyOpt.value = "";
    emptyOpt.textContent = "(first endpoint)";
    defaultSelect.appendChild(emptyOpt);
    endpoints.forEach((ep) => {
      if (!ep.name) return;
      const opt = document.createElement("option");
      opt.value = ep.name;
      opt.textContent = ep.name;
      if (ep.name === defaultEndpoint) opt.selected = true;
      defaultSelect.appendChild(opt);
    });
    defaultSelect.addEventListener("change", () => {
      defaultEndpoint = defaultSelect.value;
    });
  }

  // -- Save button ------------------------------------------------------
  const saveBtn = document.createElement("button");
  saveBtn.className = "ep-save";
  saveBtn.textContent = "Save endpoints";
  saveBtn.style.marginTop = "16px";
  wrapper.appendChild(saveBtn);

  const msgDiv = document.createElement("div");
  msgDiv.className = "ep-msg";
  wrapper.appendChild(msgDiv);

  function showMsg(text, isOk) {
    msgDiv.textContent = text;
    msgDiv.className = "ep-msg " + (isOk ? "ok" : "err");
  }

  saveBtn.addEventListener("click", async () => {
    // Validate
    for (const ep of endpoints) {
      if (!ep.name || !ep.url) {
        showMsg("Each endpoint needs both a name and a URL.", false);
        return;
      }
    }
    const endpointsJson = JSON.stringify(
      endpoints.map((ep) => ({ name: ep.name, url: ep.url }))
    );

    try {
      // Save ENDPOINTS
      const base = `/api/plugin/${slug}/settings/`;
      await fetch(`${base}ENDPOINTS/`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrfToken(),
        },
        body: JSON.stringify({ value: endpointsJson }),
      });

      // Save DEFAULT_ENDPOINT
      await fetch(`${base}DEFAULT_ENDPOINT/`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrfToken(),
        },
        body: JSON.stringify({ value: defaultEndpoint }),
      });

      showMsg("Endpoints saved successfully.", true);
    } catch (err) {
      showMsg("Failed to save: " + err.message, false);
    }
  });

  // -- CSRF helper ------------------------------------------------------
  function getCsrfToken() {
    // InvenTree stores the CSRF token in a cookie or meta tag
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.getAttribute("content");
    const cookie = document.cookie
      .split("; ")
      .find((c) => c.startsWith("csrftoken="));
    return cookie ? cookie.split("=")[1] : "";
  }

  // -- Initial render ---------------------------------------------------
  renderRows();
  renderDefaultDropdown();
}
