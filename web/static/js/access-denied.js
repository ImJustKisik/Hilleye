/* Hilleye access denial: one animation and one sound per page, with autoplay fallback. */
(function () {
  "use strict";

  var card = document.querySelector(".logs-access-denied");
  var audio = document.getElementById("logs-denied-audio");
  var replay = document.getElementById("logs-denied-replay");
  var status = document.getElementById("logs-denied-audio-status");
  if (!card || !audio || !replay || !status) return;

  var animationTimer;
  var soundTimer;
  var playing = false;
  audio.volume = 0.65;

  function animate() {
    card.classList.remove("is-denial-playing");
    void card.offsetWidth;
    card.classList.add("is-denial-playing");
    clearTimeout(animationTimer);
    animationTimer = setTimeout(function () {
      card.classList.remove("is-denial-playing");
    }, 1150);
  }

  function soundUnavailable(error) {
    playing = false;
    replay.disabled = false;
    // A user gesture can retry autoplay restrictions, but cannot repair missing media.
    replay.hidden = !error || error.name !== "NotAllowedError";
    status.textContent = replay.hidden ? "Звук недоступен." : "";
  }

  function playSound() {
    if (playing) return;
    playing = true;
    replay.disabled = true;
    status.textContent = "";
    audio.currentTime = 0;
    try {
      var result = audio.play();
      if (result && typeof result.then === "function") {
        result.then(function () {
          replay.hidden = true;
        }).catch(soundUnavailable);
      }
    } catch (error) {
      soundUnavailable(error);
    }
  }

  audio.addEventListener("ended", function () {
    playing = false;
    replay.disabled = false;
  });
  audio.addEventListener("error", function () {
    soundUnavailable(audio.error);
  });
  replay.addEventListener("click", function () {
    clearTimeout(soundTimer);
    animate();
    // Keep play() directly inside the click handler to preserve browser activation.
    playSound();
  });
  window.addEventListener("pagehide", function () {
    clearTimeout(animationTimer);
    clearTimeout(soundTimer);
    audio.pause();
  });

  animate();
  soundTimer = setTimeout(playSound, 130);
})();
