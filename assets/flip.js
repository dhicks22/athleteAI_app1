/* flip.js — place in assets/ folder
   Wires tap/click on .dial-flip to toggle .is-flipped
   Works with Dash's dynamic DOM via event delegation
*/
document.addEventListener("click", function(e) {
  const flip = e.target.closest(".dial-flip");
  if (flip) {
    flip.classList.toggle("is-flipped");
  }
});
