document.addEventListener("click", (e) => {
  const card = e.target.closest(".dial-flip");
  if (!card) return;
  card.classList.toggle("is-flipped");
});
