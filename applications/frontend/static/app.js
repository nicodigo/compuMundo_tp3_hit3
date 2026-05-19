/* ==========================================================
   Sobel Edge Detection — Frontend Application JavaScript
   ========================================================== */

(function () {
    "use strict";

    // ---- State ----
    let STATE = {
        imageId: null,
        eventSource: null,
        pollTimer: null,
        totalFragments: 16,
        completedFragments: 0,
        imageReady: false,
    };

    // ---- DOM refs ----
    const uploadInput = document.getElementById("upload-input");
    const uploadBtn = document.getElementById("upload-btn");
    const errorDiv = document.getElementById("error");
    const statusSection = document.getElementById("status-section");
    const statusText = document.getElementById("status");
    const progressFill = document.getElementById("progress-fill");
    const progressLabel = document.getElementById("progress-label");
    const resultSection = document.getElementById("result-section");
    const downloadLink = document.getElementById("download-link");

    // ---- Init ----
    let BACKEND_URL = "";

    async function init() {
        try {
            const resp = await fetch("/config");
            const cfg = await resp.json();
            BACKEND_URL = cfg.backend_url;
            uploadBtn.disabled = false;
        } catch (err) {
            showError("Failed to load configuration. Is the backend running?");
        }
    }

    // ---- Event listeners ----
    uploadInput.addEventListener("change", function () {
        uploadBtn.disabled = !this.files || this.files.length === 0;
    });

    uploadBtn.addEventListener("click", function () {
        const file = uploadInput.files[0];
        if (!file) return;

        // Validate file type
        if (!file.type.startsWith("image/png")) {
            showError("Only PNG images are accepted.");
            return;
        }

        // Validate file size (10MB)
        if (file.size > 10 * 1024 * 1024) {
            showError("File too large. Maximum size is 10MB.");
            return;
        }

        hideError();
        uploadImage(file);
    });

    // ---- Upload ----
    async function uploadImage(file) {
        setStatus("Uploading...");
        setProgress(0, 16);

        try {
            const formData = new FormData();
            formData.append("file", file);

            const resp = await fetch(BACKEND_URL + "/api/images", {
                method: "POST",
                body: formData,
            });

            if (!resp.ok) {
                const err = await resp.json();
                showError(err.detail || "Upload failed.");
                return;
            }

            const result = await resp.json();
            STATE.imageId = result.image_id;
            STATE.totalFragments = result.total_fragments || 16;
            setStatus("Processing...");
            startSSE(STATE.imageId);
            startPolling(STATE.imageId);
        } catch (err) {
            showError("Upload failed: " + err.message);
        }
    }

    // ---- SSE ----
    function startSSE(imageId) {
        if (STATE.eventSource) {
            STATE.eventSource.close();
        }

        const es = new EventSource("/events/" + encodeURIComponent(imageId));
        STATE.eventSource = es;

        es.onmessage = function (event) {
            try {
                const data = JSON.parse(event.data);
                handleFragmentResult(data);
            } catch (e) {
                // ignore malformed messages
            }
        };

        es.onerror = function () {
            // SSE may reconnect automatically — EventSource handles this.
            // If it fails persistently, polling fallback takes over.
        };
    }

    function handleFragmentResult(data) {
        const completed = data.fragment_id !== undefined
            ? STATE.completedFragments + 1
            : STATE.completedFragments;

        STATE.completedFragments = Math.min(
            completed,
            STATE.totalFragments,
        );

        setProgress(STATE.completedFragments, STATE.totalFragments);

        if (data.status === "completed" || STATE.completedFragments >= STATE.totalFragments) {
            STATE.imageReady = true;
            setStatus("Completed!");
            stopSSE();
            stopPolling();
            fetchDownloadLink();
        }
    }

    function stopSSE() {
        if (STATE.eventSource) {
            STATE.eventSource.close();
            STATE.eventSource = null;
        }
    }

    // ---- Polling fallback ----
    function startPolling(imageId) {
        STATE.pollTimer = setInterval(async function () {
            if (STATE.imageReady) {
                stopPolling();
                return;
            }
            try {
                const resp = await fetch(
                    BACKEND_URL + "/api/images/" + encodeURIComponent(imageId) + "/status"
                );
                if (!resp.ok) return;
                const data = await resp.json();
                STATE.totalFragments = data.total_fragments || 16;
                STATE.completedFragments = data.fragments_completed || 0;
                setProgress(STATE.completedFragments, STATE.totalFragments);
                if (data.status === "completed") {
                    STATE.imageReady = true;
                    setStatus("Completed!");
                    stopPolling();
                    fetchDownloadLink();
                }
            } catch (e) {
                // network errors during polling are expected
            }
        }, 3000);
    }

    function stopPolling() {
        if (STATE.pollTimer) {
            clearInterval(STATE.pollTimer);
            STATE.pollTimer = null;
        }
    }

    // ---- Download ----
    async function fetchDownloadLink() {
        try {
            const resp = await fetch(
                BACKEND_URL + "/api/images/" + encodeURIComponent(STATE.imageId) + "/result"
            );
            if (!resp.ok) return;
            const data = await resp.json();
            downloadLink.href = data.signed_url;
            downloadLink.target = "_blank";
            resultSection.hidden = false;
        } catch (e) {
            showError("Failed to get download link: " + e.message);
        }
    }

    // ---- UI helpers ----
    function setProgress(completed, total) {
        const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
        progressFill.style.width = pct + "%";
        progressLabel.textContent = completed + " / " + total + " fragments";
        statusSection.hidden = false;
    }

    function setStatus(msg) {
        statusText.textContent = msg;
    }

    function showError(msg) {
        errorDiv.textContent = msg;
        errorDiv.hidden = false;
    }

    function hideError() {
        errorDiv.textContent = "";
        errorDiv.hidden = true;
    }

    // ---- Start ----
    document.addEventListener("DOMContentLoaded", init);
})();
