const preferenceStore = (key, value) => { try { localStorage.setItem(key,value); } catch (_) {} };
const sizeControl = document.querySelector('#text-size');
if (sizeControl) {
  sizeControl.value = document.documentElement.style.fontSize.replace('%','') || '100';
  sizeControl.addEventListener('change', () => {
    document.documentElement.style.fontSize = sizeControl.value + '%';
    preferenceStore('wildtrack-text-size',sizeControl.value);
  });
}
const contrastControl = document.querySelector('#high-contrast');
if (contrastControl) {
  contrastControl.checked = document.documentElement.dataset.contrast === 'high';
  contrastControl.addEventListener('change', () => {
    document.documentElement.dataset.contrast = contrastControl.checked ? 'high' : 'standard';
    preferenceStore('wildtrack-contrast', document.documentElement.dataset.contrast);
  });
}
document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); });
});
document.querySelector('[data-print]')?.addEventListener('click', () => window.print());
const photoInput = document.querySelector('#photo');
let previewUrl;
photoInput?.addEventListener('change', () => {
  const image = document.querySelector('#photo-preview');
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  const file = photoInput.files[0];
  image.hidden = true;
  photoInput.setCustomValidity('');
  if (!file) return;
  if (file.size > 5*1024*1024) {
    photoInput.setCustomValidity('Choose an image smaller than 5 MB.');
    photoInput.reportValidity(); return;
  }
  photoInput.setCustomValidity('');
  previewUrl = URL.createObjectURL(file); image.src = previewUrl; image.hidden = false;
});
document.addEventListener('keydown', event => {
  if(event.key==='Escape') {
    document.querySelectorAll('details[open]').forEach(item=>item.open=false);
    document.querySelector('.nav-menu')?.classList.remove('open');
    document.querySelector('.menu-toggle')?.setAttribute('aria-expanded','false');
  }
});
