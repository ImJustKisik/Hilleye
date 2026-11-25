/* Keep the sign-in page active so its click can enable the denial sound. */
(function () {
  "use strict";

  const channelPrefix = "hilleye-discord-login:";
  const popupResult = document.querySelector("[data-oauth-popup]");
  const alert = document.getElementById("logs-login-error");

  if (!window.BroadcastChannel) return;

  if (popupResult) {
    const channel = new BroadcastChannel(channelPrefix + popupResult.dataset.oauthPopup);
    channel.addEventListener("message", function (event) {
      if (event.data !== "received") return;
      channel.close();
      window.close();
    });
    channel.postMessage({
      status: popupResult.dataset.oauthResult,
      errorMessage: alert?.textContent || "",
    });
    return;
  }

  const link = document.getElementById("logs-discord-login");
  if (!link || !window.AudioContext || !window.crypto?.randomUUID) return;
  if (new URL(link.dataset.oauthCallback, location.href).origin !== location.origin) return;

  const manual = document.getElementById("logs-denied-play");
  const soundUrl = link.dataset.deniedSound;
  const volume = 0.65;
  let stopAttempt;

  if (!document.getElementById("logs-denied-audio")) {
    let fallbackAudio;
    manual.querySelector("button").addEventListener("click", function () {
      fallbackAudio ??= new Audio(soundUrl);
      fallbackAudio.volume = volume;
      fallbackAudio.play().catch(function () {});
    });
  }

  link.addEventListener("click", function (event) {
    if (event.defaultPrevented || event.button !== 0 || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    const audio = new AudioContext();
    audio.resume().catch(function () {});
    const popup = window.open("about:blank", "_blank", "popup,width=560,height=650");
    if (!popup) {
      audio.close().catch(function () {});
      return;
    }
    event.preventDefault();
    stopAttempt?.();
    alert.hidden = true;
    manual.hidden = true;

    const attempt = crypto.randomUUID();
    const loginUrl = new URL(link.href);
    loginUrl.searchParams.set("popup_attempt", attempt);
    const channel = new BroadcastChannel(channelPrefix + attempt);
    const sound = fetch(soundUrl)
      .then(response => response.arrayBuffer())
      .then(data => audio.decodeAudioData(data))
      .catch(() => null);

    function cleanup() {
      window.clearTimeout(expiry);
      channel.close();
      audio.close().catch(function () {});
      if (stopAttempt === cleanup) stopAttempt = null;
    }

    const expiry = window.setTimeout(cleanup, 600000);
    stopAttempt = cleanup;
    channel.addEventListener("message", async function (event) {
      const result = event.data;
      if (!result || !["signed_in", "mfa_required", "unauthenticated"].includes(result.status)) return;
      channel.postMessage("received");
      channel.close();
      if (result.status === "signed_in") {
        cleanup();
        window.location.assign(loginUrl.searchParams.get("next") || "/logs");
        return;
      }

      alert.textContent = result.errorMessage;
      alert.hidden = false;
      if (result.status !== "mfa_required") {
        cleanup();
        return;
      }

      const buffer = await sound;
      if (stopAttempt !== cleanup) return;
      if (!buffer || audio.state !== "running") {
        manual.hidden = false;
        cleanup();
        return;
      }

      const gain = audio.createGain();
      gain.gain.value = volume;
      gain.connect(audio.destination);
      const source = audio.createBufferSource();
      source.buffer = buffer;
      source.connect(gain);
      source.addEventListener("ended", cleanup, { once: true });
      source.start();
    });

    popup.opener = null;
    popup.location.href = loginUrl.href;
  });

  window.addEventListener("pagehide", function () { stopAttempt?.(); });
})();
