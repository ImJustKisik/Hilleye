/* Attempt the denial sound once and quietly handle autoplay or media errors. */
(function () {
  "use strict";

  const audio = document.getElementById("logs-denied-audio");
  if (!audio) return;

  audio.volume = 0.65;
  const soundTimer = window.setTimeout(function () {
    audio.play().catch(function () {});
  }, 130);

  window.addEventListener("pagehide", function () {
    window.clearTimeout(soundTimer);
    audio.pause();
  }, { once: true });
})();
