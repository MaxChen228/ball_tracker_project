// === boot + nav offset ===
(function () {
  console.info('[ball_tracker] dashboard JS boot', { build: 'preview-refactor-v2' });
  const navEl = document.querySelector('.nav');
  const rootStyle = document.documentElement && document.documentElement.style;
  function syncNavOffset() {
    if (!navEl || !rootStyle) return;
    const h = Math.ceil(navEl.getBoundingClientRect().height || 0);
    if (h > 0) rootStyle.setProperty('--nav-offset', `${h}px`);
  }
  syncNavOffset();
  if (typeof ResizeObserver !== 'undefined' && navEl) {
    const navObserver = new ResizeObserver(() => syncNavOffset());
    navObserver.observe(navEl);
  } else {
    window.addEventListener('resize', syncNavOffset, { passive: true });
  }

