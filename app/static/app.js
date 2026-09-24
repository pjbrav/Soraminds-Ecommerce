/* SpiceHub Menu Manager — light vanilla JS wiring for the owner dashboard. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

async function api(method, url, body) {
  const resp = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const msg = data.error ? data.error.message : data.detail || `Request failed (${resp.status})`;
    throw new Error(msg);
  }
  return data;
}

/* ----------------------------- Upload page ----------------------------- */
const uploadBtn = $("#upload-btn");
if (uploadBtn) {
  uploadBtn.addEventListener("click", async () => {
    const tenant = $("#tenant").value;
    const file = $("#file").files[0];
    const photos = $("#photos").files;
    const alertBox = $("#upload-alert");
    const status = $("#upload-status");
    alertBox.classList.add("hidden");

    if (!file) {
      showAlert(alertBox, "Pick your menu spreadsheet first (.xlsx or .csv).", "warn");
      return;
    }
    uploadBtn.disabled = true;
    uploadBtn.textContent = "Processing…";
    status.classList.remove("hidden");

    const form = new FormData();
    form.append("file", file);
    for (const p of photos) form.append("photos", p);

    try {
      const resp = await fetch(`/tenants/${tenant}/menu/upload`, { method: "POST", body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error ? data.error.message : "Upload failed");
      status.textContent = `Processed ${data.rows} items — ${data.counts.error} error(s), ${data.counts.warning} warning(s).`;
      window.location = `/tenants/${tenant}/uploads/${data.uploadId}/report`;
    } catch (err) {
      showAlert(alertBox, err.message, "error");
      uploadBtn.disabled = false;
      uploadBtn.textContent = "Process my menu";
      status.classList.add("hidden");
    }
  });
  // Keep the kiosk / versions links tenant-aware.
  const t = $("#tenant").value;
  const k = $("#kiosk-link"), v = $("#versions-link");
  if (k && v) {
    k.href = `/tenants/${t}/kiosk`;
    v.href = `/tenants/${t}/versions`;
    k.classList.remove("hidden");
    v.classList.remove("hidden");
  }
}

function showAlert(box, message, kind) {
  box.className =
    "mb-4 rounded-xl px-4 py-3 text-sm " +
    (kind === "error"
      ? "bg-red-50 border border-red-300 text-red-800"
      : "bg-amber-50 border border-amber-300 text-amber-900");
  box.textContent = message;
  box.classList.remove("hidden");
}

/* -------------------------- Validation report -------------------------- */
const reportBody = $$("body").some ? document.body : document;
if ($(".row-save")) {
  // Inline single-row edit -> PATCH -> refresh row badge in place.
  $$(".row-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".row-card");
      const { row, tenant, upload } = btn.dataset;
      const changes = {};
      $$(".edit-input", card).forEach((input) => {
        const field = input.dataset.field;
        if (input.value !== (input.defaultValue ?? input.value)) changes[field] = input.value;
      });
      $$(".edit-check", card).forEach((chk) => {
        changes[chk.dataset.field] = chk.checked ? "Yes" : "No";
      });
      if (!Object.keys(changes).length) {
        showAlertInline(card, "No changes to save.");
        return;
      }
      btn.disabled = true;
      try {
        const result = await api(
          "PATCH",
          `/tenants/${tenant}/menu/uploads/${upload}/items/${row}`,
          { changes }
        );
        updateRowCard(card, result);
        updateSummary(result.report);
        const saved = $(".row-saved", card);
        saved.classList.remove("hidden");
        setTimeout(() => saved.classList.add("hidden"), 2500);
      } catch (err) {
        showAlertInline(card, err.message, true);
      } finally {
        btn.disabled = false;
      }
    });
  });

  // Suggested-category quick buttons from the issue text.
  $$(".cat-fix").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".row-card");
      const { row, tenant, upload } = btn.dataset;
      const select = $(`.edit-input[data-field="category"]`, card);
      select.value = btn.dataset.category;
      const result = await api(
        "PATCH",
        `/tenants/${tenant}/menu/uploads/${upload}/items/${row}`,
        { changes: { category: btn.dataset.category } }
      );
      updateRowCard(card, result);
      updateSummary(result.report);
    });
  });

  // Photo-conflict quick resolution.
  $$(".conflict-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".row-card");
      const { row, tenant, upload } = btn.dataset;
      const result = await api(
        "PATCH",
        `/tenants/${tenant}/menu/uploads/${upload}/items/${row}`,
        { changes: { price: btn.dataset.price } }
      );
      updateRowCard(card, result);
      updateSummary(result.report);
    });
  });

  // Orphan-photo assignment gallery.
  $$(".photo-assign").forEach((sel) => {
    sel.addEventListener("change", async () => {
      const tenant = $(".publish-btn")?.dataset.tenant || new URLSearchParams(location.search).get("t");
      const upload = $(".publish-btn")?.dataset.upload || location.pathname.split("/")[4];
      const tenantEl = $("#publish-btn");
      if (!sel.value) return;
      await api(
        "POST",
        `/tenants/${tenantEl.dataset.tenant}/menu/uploads/${tenantEl.dataset.upload}/photos/${sel.dataset.filename}/assign`,
        { row_index: parseInt(sel.value, 10) }
      );
      location.reload();
    });
  });

  // Publish.
  const publishBtn = $("#publish-btn");
  if (publishBtn) {
    publishBtn.addEventListener("click", async () => {
      publishBtn.disabled = true;
      try {
        const result = await api(
          "POST",
          `/tenants/${publishBtn.dataset.tenant}/menu/uploads/${publishBtn.dataset.upload}/publish`
        );
        window.location = `/tenants/${publishBtn.dataset.tenant}/versions?published=${result.version}`;
      } catch (err) {
        showAlertInline(document.body, err.message, true);
        publishBtn.disabled = false;
      }
    });
  }

  // Partial re-upload merge.
  const partial = $("#partial-file");
  if (partial) {
    partial.addEventListener("change", async () => {
      const publishBtn = $("#publish-btn");
      const form = new FormData();
      form.append("file", partial.files[0]);
      const resp = await fetch(
        `/tenants/${publishBtn.dataset.tenant}/menu/uploads/${publishBtn.dataset.upload}/partial-reupload`,
        { method: "POST", body: form }
      );
      const data = await resp.json();
      if (!resp.ok) {
        alert(data.error ? data.error.message : "Partial re-upload failed");
        return;
      }
      location.reload();
    });
  }
}

function updateRowCard(card, result) {
  const badge = $(".row-badge", card);
  badge.className =
    "row-badge rounded-full px-2.5 py-0.5 text-xs font-bold " +
    (result.status === "valid"
      ? "bg-green-100 text-green-800"
      : result.status === "warning"
      ? "bg-amber-100 text-amber-800"
      : "bg-red-100 text-red-800");
  badge.textContent =
    result.status === "valid" ? "✓ Valid" : result.status === "warning" ? "⚠ Warning" : "✗ Error";
}

function updateSummary(report) {
  const el = $("#hard-count");
  if (el && report) {
    el.textContent = `${report.hard_errors} blocking issue${report.hard_errors === 1 ? "" : "s"}`;
    el.className =
      "rounded-full px-3 py-1 " +
      (report.publishable ? "bg-green-100 text-green-800" : "bg-red-600 text-white");
    const publishBtn = $("#publish-btn");
    const previewLink = $("#preview-btn");
    if (report.publishable) {
      if (publishBtn) publishBtn.disabled = false;
      if (previewLink) {
        previewLink.classList.remove("bg-neutral-200", "text-neutral-400", "pointer-events-none");
        previewLink.classList.add("bg-white", "border-2", "border-[#eb1700]", "text-[#eb1700]");
      }
    }
  }
}

function showAlertInline(context, message, isError) {
  let box = $("#inline-alert");
  if (!box) {
    box = document.createElement("div");
    box.id = "inline-alert";
    box.className = "fixed bottom-4 right-4 rounded-xl px-4 py-3 text-sm shadow-lg z-50";
    document.body.appendChild(box);
  }
  box.className =
    "fixed bottom-4 right-4 rounded-xl px-4 py-3 text-sm shadow-lg z-50 " +
    (isError ? "bg-red-600 text-white" : "bg-neutral-900 text-white");
  box.textContent = message;
  setTimeout(() => box.remove(), 4000);
}

/* --------------------------- Preview page ------------------------------ */
const publishPage = $("#publish-btn-page");
if (publishPage) {
  publishPage.addEventListener("click", async () => {
    // The upload id is not on this page; publish from the versions of the
    // last upload via the report page. Simplest: go back and use its button.
    publishPage.disabled = true;
    const uploadId = new URLSearchParams(location.search).get("u");
    if (uploadId) {
      try {
        await api("POST", `/tenants/${tenantFromPath()}/menu/uploads/${uploadId}/publish`);
        location.href = `/tenants/${tenantFromPath()}/versions`;
      } catch (err) {
        alert(err.message);
        publishPage.disabled = false;
      }
      return;
    }
    history.back();
  });
}
function tenantFromPath() {
  return location.pathname.split("/")[2] || "spicehub-kitchen-troy";
}

/* --------------------------- Versions page ----------------------------- */
$$(".rollback-btn").forEach((btn) => {
  btn.addEventListener("click", async () => {
    try {
      const result = await api(
        "POST",
        `/tenants/${btn.dataset.tenant}/menu/versions/${btn.dataset.version}/rollback`
      );
      alert(result.message);
      location.reload();
    } catch (err) {
      alert(err.message);
    }
  });
});
