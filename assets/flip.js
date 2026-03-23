/* flip.js — assets/flip.js
   - Mutual exclusion: only one dial open at a time
   - Auto-close after 5 seconds
   - Tap outside = close all
   - Tap same dial = toggle closed
*/
(function () {
  var closeTimer = null;

  function closeAll() {
    document.querySelectorAll(".dial-flip.is-flipped").forEach(function (el) {
      el.classList.remove("is-flipped");
    });
    clearTimeout(closeTimer);
    closeTimer = null;
  }

  document.addEventListener("click", function (e) {
    var flip = e.target.closest(".dial-flip");

    if (!flip) {
      closeAll();
      return;
    }

    var wasOpen = flip.classList.contains("is-flipped");
    closeAll();

    if (!wasOpen) {
      flip.classList.add("is-flipped");
      closeTimer = setTimeout(function () {
        flip.classList.remove("is-flipped");
        closeTimer = null;
      }, 5000);
    }
  });
})();