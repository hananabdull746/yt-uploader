document.addEventListener('submit', (event) => {
  const form = event.target;
  if (form.matches('[data-confirm]') && !window.confirm(form.dataset.confirm)) event.preventDefault();
});
