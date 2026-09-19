try {
  const preferencesRoot = document.documentElement;
  const theme = localStorage.getItem('wildtrack-theme');
  if (theme === 'dark' || (!theme && window.matchMedia('(prefers-color-scheme: dark)').matches)) preferencesRoot.dataset.theme = 'dark';
  const size = localStorage.getItem('wildtrack-text-size');
  if (['100','115','130'].includes(size)) preferencesRoot.style.fontSize = size + '%';
  if (localStorage.getItem('wildtrack-contrast') === 'high') preferencesRoot.dataset.contrast = 'high';
} catch (_) { /* Preferences remain usable when storage is unavailable. */ }
