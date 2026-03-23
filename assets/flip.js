/* flip.js — v12
   Simple modal. No 3D CSS involved for back content.
   Back content is hidden via JS (not CSS) so cache issues don't matter.
*/
(function () {
  var modal, backdrop, contentEl, autoClose;

  function build() {
    if (document.getElementById("dial-modal")) return;

    // Hide ALL back content immediately — belt and braces
    hideAllBackContent();

    backdrop = document.createElement("div");
    backdrop.id = "dial-backdrop";
    backdrop.style.cssText = [
      "display:none",
      "position:fixed",
      "inset:0",
      "background:rgba(0,0,0,0.40)",
      "z-index:9000",
      "-webkit-backdrop-filter:blur(3px)",
      "backdrop-filter:blur(3px)",
    ].join(";");
    backdrop.addEventListener("click", hide);
    document.body.appendChild(backdrop);

    modal = document.createElement("div");
    modal.id = "dial-modal";
    modal.style.cssText = [
      "display:none",
      "position:fixed",
      "top:50%",
      "left:50%",
      "transform:translate(-50%,-50%)",
      "z-index:9001",
      "width:min(340px,90vw)",
      "max-height:70vh",
      "overflow-y:auto",
      "-webkit-overflow-scrolling:touch",
      "padding:22px 20px 18px 20px",
      "border-radius:20px",
      "background:#fff",
      "border:1px solid rgba(0,0,0,0.10)",
      "box-shadow:0 8px 40px rgba(0,0,0,0.28)",
      "font-family:-apple-system,BlinkMacSystemFont,'Inter',sans-serif",
      "font-size:14px",
      "line-height:1.55",
      "color:#1a1a1a",
      "flex-direction:column",
      "box-sizing:border-box",
    ].join(";");
    modal.addEventListener("click", function(e){ e.stopPropagation(); });

    // Close button
    var x = document.createElement("button");
    x.textContent = "✕  Tap to close";
    x.style.cssText = [
      "display:block",
      "margin-bottom:14px",
      "background:none",
      "border:none",
      "font-size:12px",
      "opacity:0.45",
      "cursor:pointer",
      "padding:0",
      "color:inherit",
    ].join(";");
    x.addEventListener("click", hide);
    modal.appendChild(x);

    contentEl = document.createElement("div");
    contentEl.id = "dial-modal-inner";
    modal.appendChild(contentEl);

    document.body.appendChild(modal);
  }

  function hideAllBackContent() {
    // Force-hide all back content regardless of CSS
    document.querySelectorAll(".dial-back-content").forEach(function(el) {
      el.style.display = "none";
    });
  }

  function show(flipEl) {
    build();
    hideAllBackContent();

    var src = flipEl.querySelector(".dial-back-content");
    if (!src) return;

    contentEl.innerHTML = src.innerHTML;

    // Style inner elements
    var title = contentEl.querySelector(".dial-back-title");
    if (title) {
      title.style.cssText = "display:block;font-weight:800;font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:#1e88e5;margin:0 0 12px 0;";
    }

    var body = contentEl.querySelector(".dial-back-body");
    if (body) {
      body.style.cssText = "display:block;font-size:14px;line-height:1.6;color:#1a1a1a;";
      body.querySelectorAll("div").forEach(function(d){ d.style.marginBottom="4px"; });
    }

    var hint = contentEl.querySelector(".dial-back-hint");
    if (hint) {
      hint.style.cssText = "display:block;font-size:11px;opacity:0.4;margin-top:14px;";
    }

    var recos = contentEl.querySelectorAll(".dial-back-reco");
    recos.forEach(function(r){
      r.style.cssText = "display:block;font-size:13px;font-weight:700;color:#1e88e5;margin-top:8px;";
    });

    backdrop.style.display = "block";
    modal.style.display    = "flex";

    clearTimeout(autoClose);
    autoClose = setTimeout(hide, 10000);
  }

  function hide() {
    if (modal)    modal.style.display    = "none";
    if (backdrop) backdrop.style.display = "none";
    clearTimeout(autoClose);
  }

  // Delegation — works after Dash re-renders
  document.addEventListener("click", function(e) {
    var flip = e.target.closest(".dial-flip");
    if (flip) { show(flip); return; }
    // click outside modal closes it
    if (modal && modal.style.display === "flex") hide();
  });

  // Run hideAllBackContent after every Dash render
  var observer = new MutationObserver(function() {
    hideAllBackContent();
  });

  function init() {
    build();
    hideAllBackContent();
    observer.observe(document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

})();