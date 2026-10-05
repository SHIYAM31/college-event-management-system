(function () {
  const reader = document.getElementById("reader");
  const result = document.getElementById("result");
  const presentEl = document.getElementById("present");
  let busy = false;

  async function send(token) {
    if (busy || !token) return;
    busy = true;
    try {
      const res = await fetch(reader.dataset.url, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": reader.dataset.csrf },
        body: JSON.stringify({ token: token, event_id: reader.dataset.event }),
      });
      const data = await res.json();
      result.hidden = false;
      result.className = "flash " + (data.status === "ok" ? "success" : "error");
      result.textContent = data.message;
      if (data.status === "ok") presentEl.textContent = data.present;
    } catch (e) {
      result.hidden = false;
      result.className = "flash error";
      result.textContent = "Network error. Try again.";
    }
    setTimeout(function () { busy = false; }, 1500);
  }

  document.getElementById("manual-btn").addEventListener("click", function () {
    const input = document.getElementById("manual");
    send(input.value.trim());
    input.value = "";
  });

  if (window.Html5Qrcode) {
    const scanner = new Html5Qrcode("reader");
    scanner.start({ facingMode: "environment" }, { fps: 10, qrbox: 240 },
      function (text) { send(text); }, function () {}).catch(function () {
        result.hidden = false;
        result.className = "flash error";
        result.textContent = "Camera not available. Use the manual code box.";
      });
  }
})();
