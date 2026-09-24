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

/* Tenant + upload id for the current page. Derived from the URL path
   (/tenants/{t}/uploads/{u}/report|preview) so every button on the page —
   including quick-fix buttons that only carry data-row — shares it. */
function pageCtx() {
  const parts = location.pathname.split("/").filter(Boolean); // [tenants, t, uploads, u, page]
  if (parts[0] === "tenants" && parts[2] === "uploads") {
    return { tenant: parts[1], upload: parts[3] };
  }
  const btn = $("#publish-btn");
  if (btn && btn.dataset.tenant) {
    return { tenant: btn.dataset.tenant, upload: btn.dataset.upload };
  }
  return { tenant: null, upload: null };
}

function patchRow(row, changes) {
  const { tenant, upload } = pageCtx();
  return api("PATCH", `/tenants/${tenant}/menu/uploads/${upload}/items/${row}`, { changes });
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
if ($(".row-save")) {
  // Remember each control's initial value so we only send real edits.
  // (Selects have no `defaultValue` like text inputs do — this covers them.)
  $$(".edit-input").forEach((input) => {
    input.dataset.initial = input.value;
  });

  // Inline single-row edit -> PATCH -> update the row badge in place.
  $$(".row-save").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".row-card");
      const { row } = btn.dataset;
      const changes = {};
      $$(".edit-input", card).forEach((input) => {
        if (input.value !== input.dataset.initial) changes[input.dataset.field] = input.value;
      });
      $$(".edit-check", card).forEach((chk) => {
        changes[chk.dataset.field] = chk.checked ? "Yes" : "No";
      });
      if (!Object.keys(changes).length) {
        showAlertInline("No changes to save.");
        return;
      }
      btn.disabled = true;
      try {
        const result = await patchRow(row, changes);
        // Remember the saved values so the next Save only sends new edits.
        $$(".edit-input", card).forEach((input) => {
          input.dataset.initial = input.value;
        });
        const saved = $(".row-saved", card);
        saved.classList.remove("hidden");
        setTimeout(() => saved.classList.add("hidden"), 2500);
        afterRowUpdate(card, result);
      } catch (err) {
        showAlertInline(err.message, true);
      } finally {
        btn.disabled = false;
      }
    });
  });

  // Suggested-category quick-fix buttons from the issue text.
  $$(".cat-fix").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".row-card");
      const { row } = btn.dataset;
      const select = $(`.edit-input[data-field="category"]`, card);
      if (select) select.value = btn.dataset.category;
      try {
        const result = await patchRow(row, { category: btn.dataset.category });
        afterRowUpdate(card, result);
      } catch (err) {
        showAlertInline(err.message, true);
      }
    });
  });

  // Add item: owner creates a new blank row, then fills it in like any other.
  const addBtn = $("#add-item-btn");
  if (addBtn) {
    addBtn.addEventListener("click", async () => {
      addBtn.disabled = true;
      const { tenant, upload } = pageCtx();
      try {
        await api("POST", `/tenants/${tenant}/menu/uploads/${upload}/items`);
        location.reload();
      } catch (err) {
        showAlertInline(err.message, true);
        addBtn.disabled = false;
      }
    });
  }

  // Delete row: owner removes an item outright (e.g. one of two duplicates).
  $$(".row-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("Delete this row? The item is removed from this upload "
                   + "(already-published versions are not affected).")) return;
      const { tenant, upload } = pageCtx();
      const { row } = btn.dataset;
      btn.disabled = true;
      try {
        await api("DELETE", `/tenants/${tenant}/menu/uploads/${upload}/items/${row}`);
        location.reload();
      } catch (err) {
        showAlertInline(err.message, true);
        btn.disabled = false;
      }
    });
  });

  // Per-row photo upload (multipart, so not via the JSON api() helper).
  $$(".row-photo input").forEach((inp) => {
    inp.addEventListener("change", async () => {
      const f = inp.files[0];
      if (!f) return;
      const label = inp.closest(".row-photo");
      const { tenant, upload } = pageCtx();
      const { row } = label.dataset;
      const fd = new FormData();
      fd.append("file", f);
      try {
        const resp = await fetch(
          `/tenants/${tenant}/menu/uploads/${upload}/items/${row}/photo`,
          { method: "POST", body: fd },
        );
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          const msg = data.error ? data.error.message : `Upload failed (${resp.status})`;
          throw new Error(msg);
        }
        location.reload();
      } catch (err) {
        showAlertInline(err.message, true);
      } finally {
        inp.value = "";
      }
    });
  });

  // Photo-conflict quick resolution: owner picks which source is right.
  $$(".conflict-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".row-card");
      const { row } = btn.dataset;
      const priceInput = $(`.edit-input[data-field="price"]`, card);
      const { tenant, upload } = pageCtx();
      btn.disabled = true;
      try {
        if (btn.dataset.action === "keep_sheet") {
          // Keeping the spreadsheet value is an explicit dismissal, not an
          // edit: re-sending the same price would just re-raise the conflict.
          await api("POST", `/tenants/${tenant}/menu/uploads/${upload}/items/${row}/dismiss-conflict`);
          location.reload();
          return;
        }
        if (priceInput) priceInput.value = btn.dataset.price;
        const result = await patchRow(row, { price: btn.dataset.price });
        afterRowUpdate(card, result);
      } catch (err) {
        showAlertInline(err.message, true);
        btn.disabled = false;
      }
    });
  });

  // Orphan-photo assignment gallery.
  $$(".photo-assign").forEach((sel) => {
    sel.addEventListener("change", async () => {
      if (!sel.value) return;
      const { tenant, upload } = pageCtx();
      try {
        await api(
          "POST",
          `/tenants/${tenant}/menu/uploads/${upload}/photos/${sel.dataset.filename}/assign`,
          { row_index: parseInt(sel.value, 10) }
        );
        location.reload();
      } catch (err) {
        showAlertInline(err.message, true);
      }
    });
  });

  // Publish.
  const publishBtn = $("#publish-btn");
  if (publishBtn) {
    publishBtn.addEventListener("click", async () => {
      publishBtn.disabled = true;
      const { tenant, upload } = pageCtx();
      try {
        const result = await api("POST", `/tenants/${tenant}/menu/uploads/${upload}/publish`);
        window.location = `/tenants/${tenant}/versions?published=${result.version}`;
      } catch (err) {
        showAlertInline(err.message, true);
        publishBtn.disabled = false;
      }
    });
  }

  // Partial re-upload merge (correction path b).
  const partial = $("#partial-file");
  if (partial) {
    partial.addEventListener("change", async () => {
      if (!partial.files.length) return;
      const { tenant, upload } = pageCtx();
      const form = new FormData();
      form.append("file", partial.files[0]);
      const resp = await fetch(
        `/tenants/${tenant}/menu/uploads/${upload}/partial-reupload`,
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

/* Post-mutation refresh. The badge + summary always update; if the row is
   now valid its issue cards are removed in place, but if issues REMAIN the
   page reloads so the current, server-computed issue list is shown — never
   leave a stale issue card on screen. */
function afterRowUpdate(card, result) {
  updateRowCard(card, result);
  updateSummary(result.report);
  if (result.status === "valid") {
    const issues = $(".row-issues", card);
    if (issues) issues.remove();
  } else {
    location.reload();
    return;
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

function showAlertInline(message, isError) {
  let box = $("#inline-alert");
  if (!box) {
    box = document.createElement("div");
    box.id = "inline-alert";
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
    publishPage.disabled = true;
    const { tenant, upload } = pageCtx(); // /tenants/{t}/uploads/{u}/preview
    if (!tenant || !upload) {
      history.back(); // fall back to the report page's publish button
      return;
    }
    try {
      await api("POST", `/tenants/${tenant}/menu/uploads/${upload}/publish`);
      location.href = `/tenants/${tenant}/versions`;
    } catch (err) {
      showAlertInline(err.message, true);
      publishPage.disabled = false;
    }
  });
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
