// Minimal client helpers. No framework, no build step — this is a single-user
// local app and the UI surface is small enough that vanilla JS is less machinery
// than a bundler would be.

function toast(message, isError = false) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = message;
  el.classList.toggle("error", Boolean(isError));
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, 4500);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });

  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { detail: text }; }
  }

  if (!response.ok) {
    // The API reports gate failures as {"error": "...", ...} per AC-2/AC-3.
    const err = new Error(describeError(payload, response.status));
    err.payload = payload;
    err.status = response.status;
    throw err;
  }
  return payload;
}

function describeError(payload, status) {
  if (!payload) return `Request failed (${status})`;
  if (payload.error === "phase_not_ready") {
    return `Not ready: approval for "${payload.required_approval}" is required first.`;
  }
  if (payload.error === "same_harness_not_allowed") {
    return "Implement and review must use different harnesses.";
  }
  if (payload.error === "artifact_missing") {
    return `Artifact not found yet: ${payload.artifact_path}`;
  }
  if (payload.error === "invalid_transition") {
    return `Cannot move from "${payload.current_phase}" to "${payload.requested_phase}".`;
  }
  if (payload.error === "not_a_git_repo") {
    return `Not a git repository: ${payload.repo_path}`;
  }
  if (payload.error === "source_modified") {
    return `The planning run modified source files, which it must not: ${payload.paths.join(", ")}`;
  }
  if (payload.error === "harness_error") {
    return `Harness failed: ${payload.detail}`;
  }
  if (payload.error === "bedrock_error") {
    return `Bedrock call failed: ${payload.detail}`;
  }
  // FastAPI wraps HTTPException details; unwrap before falling through.
  if (payload.detail && typeof payload.detail === "object" && payload.detail.error) {
    return describeError(payload.detail, status);
  }
  if (payload.error) return payload.error;
  if (typeof payload.detail === "string") return payload.detail;
  if (Array.isArray(payload.detail)) {
    return payload.detail.map((d) => d.msg || JSON.stringify(d)).join("; ");
  }
  return `Request failed (${status})`;
}

async function submitJson(form, path, method = "POST") {
  const data = Object.fromEntries(new FormData(form).entries());
  return api(path, { method, body: JSON.stringify(data) });
}

function relTime(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-reltime]").forEach((el) => {
    const iso = el.getAttribute("data-reltime");
    if (iso) el.textContent = relTime(iso);
  });
});

window.wo = { api, toast, submitJson, relTime, describeError };
