
console.log('QR Parking System loaded');

document.addEventListener("DOMContentLoaded", function () {
    const video = document.getElementById("video"); // Make sure your HTML video tag has id="video"
    const canvas = document.getElementById("canvas"); // Make sure your HTML canvas tag has id="canvas"
    const captureBtn = document.getElementById("capture-btn"); // Your "Capture & Scan" button
    const cameraForm = document.getElementById("camera-form"); // The wrapper form element
    const hiddenInput = document.getElementById("camera_image"); // The hidden form field
    const loadingSpinner = document.getElementById("loading-spinner"); // Your loading UI overlay element

    // 1. Initialize Web Camera Stream cleanly
    if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia && video) {
        navigator.mediaDevices.getUserMedia({ 
            video: { facingMode: "environment", width: { ideal: 1280 }, height: { ideal: 720 } } 
        })
        .then(function (stream) {
            video.srcObject = stream;
            video.play();
        })
        .catch(function (err) {
            console.error("Camera access blocked or unavailable: ", err);
        });
    }

    // 2. Process, Compress, and Submit Image Interception
    if (captureBtn && video && canvas && cameraForm && hiddenInput) {
        captureBtn.addEventListener("click", function (e) {
            e.preventDefault(); // Stop standard form submissions

            const context = canvas.getContext("2d");
            
            // Sync canvas dimensions with active webcam dimensions
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            
            // Draw current frame onto canvas matrix
            context.drawImage(video, 0, 0, canvas.width, canvas.height);

            /**
             * ⚡ THE FIX: Force JPEG compression!
             * canvas.toDataURL('image/png') produces massive files that trigger the 413 error.
             * Changing to 'image/jpeg' with a 0.6 quality multiplier keeps the plate sharp 
             * while slashing the payload down from 18MB to ~300KB.
             */
            const compressedDataUrl = canvas.toDataURL("image/jpeg", 0.6);

            // Populate hidden input element data string
            hiddenInput.value = compressedDataUrl;

            // 3. UI Response Feedback Protection
            captureBtn.disabled = true;
            captureBtn.innerText = "Processing OCR Image...";
            if (loadingSpinner) {
                loadingSpinner.style.display = "block"; // Turn on your custom CSS spinner loader loop
            }

            // Post configuration payload block over to Flask backend controllers
            cameraForm.submit();
        });
    }
});