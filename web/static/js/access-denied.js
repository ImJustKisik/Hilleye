/* Play the denial sound, with a manual option on the Discord sign-in screen. */
(function () {
  "use strict";

  const audio = document.getElementById("logs-denied-audio");
  if (!audio) return;

  audio.volume = 0.65;
  const manual = document.getElementById("logs-denied-play");
  if (manual) {
    manual.querySelector("button").addEventListener("click", function () {
      audio.play().catch(function () {});
    });
  }

  let soundTimer;
  if (document.querySelector("[data-oauth-popup]")) {
    if (manual) manual.hidden = false;
  } else {
    soundTimer = window.setTimeout(function () {
      audio.play().catch(function (error) {
        if (manual && error.name === "NotAllowedError") manual.hidden = false;
      });
    }, 130);
  }

  window.addEventListener("pagehide", function () {
    window.clearTimeout(soundTimer);
    audio.pause();
  }, { once: true });
})();
