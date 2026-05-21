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
    async function init() {
        uploadBtn.disabled = false;
        // Reset state on page load and after completion
        STATE.imageReady = false;
        STATE.completedFragments = 0;
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

    // Reset state before starting a new upload
    function resetState() {
        STATE.imageReady = false;
        STATE.completedFragments = 0;
        resultSection.hidden = true;
    }

    // ---- Upload ----
    async function uploadImage(file) {
        setStatus("Uploading...");
        setProgress(0, 16);

        try {
            const formData = new FormData();
            formData.append("file", file);

            const resp = await fetch("/api/images", {
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
            resetState();
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
                // Only update progress from SSE — let polling handle completion
                const count = data.fragment_id !== undefined ? 1 : 0;
                handleFragmentResult(data, count);
            } catch (e) {
                // ignore malformed messages
            }
        };

        es.onerror = function () {
            // SSE may reconnect automatically — EventSource handles this.
            // If it fails persistently, polling fallback takes over.
        };
    }

    function handleFragmentResult(data, count) {
        // Polling is the authoritative source for fragment count.
        // SSE only updates the progress bar optimistically between polls.
        if (count > 0) {
            STATE.completedFragments = Math.min(
                STATE.completedFragments + count,
                STATE.totalFragments,
            );
        }

        // Always update the bar, even if we haven't incremented
        if (!STATE.imageReady) {
            setProgress(STATE.completedFragments, STATE.totalFragments);
        }

        if (data.status === "completed" && !STATE.imageReady) {
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
        // Clear any previous polling interval before starting a new one
        stopPolling();

        STATE.pollTimer = setInterval(async function () {
            if (STATE.imageReady) {
                stopPolling();
                return;
            }
            try {
                // Only poll for the CURRENT image
                const resp = await fetch(
                    "/api/images/" + encodeURIComponent(imageId) + "/status"
                );
                // If the imageId has changed since the poll started, discard
                if (STATE.imageId !== imageId) return;
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
            // Retain the imageId at the moment of completion to avoid races
            const completedImageId = STATE.imageId;
            const resp = await fetch("/api/images/" + encodeURIComponent(completedImageId) + "/result");
            if (STATE.imageId !== completedImageId) return; // a newer upload started
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