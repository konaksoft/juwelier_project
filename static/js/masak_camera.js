/**
 * MASAK Public Form — Native Kamera Entegrasyonu
 * getUserMedia tabanlı, dış kütüphane yoktur.
 */
(function() {
    "use strict";
    const MAX_WIDTH = 1600;
    const JPEG_QUALITY = 0.85;

    function resizeAndEncode(video, canvas) {
        const vw = video.videoWidth || 1280;
        const vh = video.videoHeight || 960;
        let tw = vw, th = vh;
        if (vw > MAX_WIDTH) { tw = MAX_WIDTH; th = Math.round((vh * MAX_WIDTH) / vw); }
        canvas.width = tw; canvas.height = th;
        canvas.getContext("2d").drawImage(video, 0, 0, tw, th);
        return canvas.toDataURL("image/jpeg", JPEG_QUALITY);
    }

    function fileToResizedDataUrl(file, cb) {
        const reader = new FileReader();
        reader.onload = function(e) {
            const img = new Image();
            img.onload = function() {
                let tw = img.width, th = img.height;
                if (tw > MAX_WIDTH) { th = Math.round((th * MAX_WIDTH) / tw); tw = MAX_WIDTH; }
                const c = document.createElement("canvas");
                c.width = tw; c.height = th;
                c.getContext("2d").drawImage(img, 0, 0, tw, th);
                cb(c.toDataURL("image/jpeg", JPEG_QUALITY));
            };
            img.src = e.target.result;
        };
        reader.readAsDataURL(file);
    }

    function setupBlock(block) {
        const side = block.dataset.cam;
        const video = block.querySelector(".cam-video");
        const canvas = block.querySelector(".cam-canvas");
        const preview = block.querySelector(".preview");
        const placeholder = block.querySelector(".cam-placeholder");
        const btnStart = block.querySelector(".btn-start");
        const btnCapture = block.querySelector(".btn-capture");
        const btnRetake = block.querySelector(".btn-retake");
        const fileInput = block.querySelector(".cam-file");
        const hiddenData = document.querySelector('[name="id_' + side + '_image_data"]');
        let stream = null;

        function stopStream() {
            if (stream) { stream.getTracks().forEach(t => t.stop()); stream = null; }
        }
        function showState(state) {
            placeholder.classList.toggle("hidden", state !== "idle");
            video.classList.toggle("hidden", state !== "streaming");
            preview.classList.toggle("hidden", state !== "captured");
            btnStart.classList.toggle("hidden", state !== "idle");
            btnCapture.classList.toggle("hidden", state !== "streaming");
            btnRetake.classList.toggle("hidden", state !== "captured");
        }

        btnStart.addEventListener("click", async function() {
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                alert("Tarayıcınız kamera erişimini desteklemiyor. Lütfen 'Galeri' butonundan fotoğraf seçiniz.");
                return;
            }
            try {
                stream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: { ideal: "environment" },
                             width: { ideal: 1920 }, height: { ideal: 1080 } },
                    audio: false
                });
                video.srcObject = stream;
                await video.play();
                showState("streaming");
            } catch (err) {
                console.error("Kamera hatası:", err);
                alert("Kameraya erişilemedi: " + (err.message || err.name) +
                      "\nLütfen 'Galeri' butonundan fotoğraf seçiniz.");
            }
        });

        btnCapture.addEventListener("click", function() {
            const dataUrl = resizeAndEncode(video, canvas);
            hiddenData.value = dataUrl;
            preview.src = dataUrl;
            stopStream();
            showState("captured");
        });

        btnRetake.addEventListener("click", function() {
            hiddenData.value = "";
            preview.src = "";
            showState("idle");
        });

        fileInput.addEventListener("change", function(e) {
            const file = e.target.files && e.target.files[0];
            if (!file) return;
            fileToResizedDataUrl(file, function(dataUrl) {
                hiddenData.value = dataUrl;
                preview.src = dataUrl;
                stopStream();
                showState("captured");
            });
        });

        showState("idle");
        window.addEventListener("pagehide", stopStream);
    }

    document.addEventListener("DOMContentLoaded", function() {
        document.querySelectorAll(".camera-block").forEach(setupBlock);
    });
})();
