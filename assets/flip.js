document.addEventListener("click", (e) => {
  if (!e?.target?.closest) return;

  const home = document.getElementById("home-view");
  if (!home) return;

  const card = e.target.closest(".dial-flip");
  if (!card || !home.contains(card)) return;

  card.classList.toggle("is-flipped");
});
