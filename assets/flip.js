/* flip.js — place in assets/ folder
   Toggles .is-flipped on .dial-flip elements.
   Auto-closes any other open dial when a new one is tapped.
   Auto-closes after 4 seconds of no interaction.
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
      // Clicked outside any dial — close all
      closeAll();
      return;
    }

    var isCurrentlyFlipped = flip.classList.contains("is-flipped");

    // Close all first
    closeAll();

    if (!isCurrentlyFlipped) {
      // Open this one
      flip.classList.add("is-flipped");

      // Auto-close after 4 seconds
      closeTimer = setTimeout(function () {
        flip.classList.remove("is-flipped");
        closeTimer = null;
      }, 4000);
    }
    // If it was already open, closeAll() already shut it — so tap = toggle off
  });
})();