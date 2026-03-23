/* flip.js — assets/flip.js v10
   Back card is rendered in a separate overlay div appended to <body>,
   completely outside the 3D transform context.
   This fixes position:fixed being broken by preserve-3d.
*/
(function () {
  var overlay    = null;
  var backdrop   = null;
  var closeTimer = null;
  var activeFlip = null;

  /* ── Build overlay elements once ── */
  function init() {
    backdrop = document.createElement("div");
    backdrop.id = "dial-backdrop";
    Object.assign(backdrop.style, {
      display:         "none",
      position:        "fixed",
      inset:           "0",
      background:      "rgba(0,0,0,0.35)",
      backdropFilter:  "blur(2px)",
      zIndex:          "998",
    });
    backdrop.addEventListener("click", closeCard);
    document.body.appendChild(backdrop);

    overlay = document.createElement("div");
    overlay.id = "dial-overlay";
    Object.assign(overlay.style, {
      display:       "none",
      position:      "fixed",
      top:           "50%",
      left:          "50%",
      transform:     "translate(-50%, -50%)",
      zIndex:        "999",
      width:         "min(320px, 88vw)",
      maxHeight:     "72vh",
      overflowY:     "auto",
      WebkitOverflowScrolling: "touch",
      padding:       "20px 18px 16px 18px",
      borderRadius:  "20px",
      background:    "var(--card-bg, #fff)",
      border:        "1px solid rgba(0,0,0,0.12)",
      boxShadow:     "0 4px 6px rgba(0,0,0,0.06), 0 20px 48px rgba(0,0,0,0.22)",
      fontFamily:    "-apple-system, BlinkMacSystemFont, 'Inter', sans-serif",
    });
    overlay.addEventListener("click", function (e) { e.stopPropagation(); });
    document.body.appendChild(overlay);
  }

  function closeCard() {
    if (overlay)   overlay.style.display   = "none";
    if (backdrop)  backdrop.style.display  = "none";
    if (activeFlip) {
      activeFlip.classList.remove("is-flipped");
      activeFlip = null;
    }
    clearTimeout(closeTimer);
    closeTimer = null;
  }

  function openCard(flipEl) {
    /* Find the .dial-back-content inside this flip element */
    var content = flipEl.querySelector(".dial-back-content");
    if (!content) return;

    /* Clone content into overlay */
    overlay.innerHTML = content.innerHTML;
    overlay.style.display = "block";
    backdrop.style.display = "block";

    /* Apply dark mode if active */
    var isDark = document.documentElement.getAttribute("data-theme") === "dark";
    if (isDark) {
      overlay.style.background = "var(--card-bg, #16181c)";
      overlay.style.border     = "1px solid rgba(255,255,255,0.14)";
      overlay.style.color      = "var(--text, #f2f2f2)";
    } else {
      overlay.style.background = "var(--card-bg, #fff)";
      overlay.style.border     = "1px solid rgba(0,0,0,0.12)";
      overlay.style.color      = "var(--text, #1a1a1a)";
    }

    activeFlip = flipEl;
    flipEl.classList.add("is-flipped");

    /* Auto-close after 6 seconds */
    clearTimeout(closeTimer);
    closeTimer = setTimeout(closeCard, 6000);
  }

  /* ── Wire clicks on .dial-flip elements (delegation) ── */
  document.addEventListener("click", function (e) {
    var flip = e.target.closest(".dial-flip");

    if (!flip) {
      closeCard();
      return;
    }

    if (flip === activeFlip) {
      /* Tap same dial = close */
      closeCard();
    } else {
      /* Close any open dial first, then open new one */
      closeCard();
      openCard(flip);
    }
  });

  /* Init when DOM is ready */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  /* Re-init if Dash re-renders (MutationObserver on body) */
  var mo = new MutationObserver(function (mutations) {
    mutations.forEach(function (m) {
      m.addedNodes.forEach(function (node) {
        if (node.nodeType === 1 && node.querySelector && node.querySelector(".dial-flip")) {
          /* New dials appeared — overlay elements already exist, nothing to do */
        }
      });
    });
  });
  document.addEventListener("DOMContentLoaded", function () {
    mo.observe(document.body, { childList: true, subtree: true });
  });

})();