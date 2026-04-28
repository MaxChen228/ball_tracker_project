/* /gt page tick loop — coordinates polling.
 *
 * SSR provides a snapshot in __GT_INITIAL_STATE__; we re-render once
 * on DOMContentLoaded so JS handlers wire onto the SSR markup, then
 * poll forever:
 *   /gt/sessions  every 5 s  (5000 ms)
 *   /gt/queue     every 1 s  (1000 ms)
 *
 * Session list re-renders on its own filter input changes (user typing)
 * and on each /gt/sessions response. Queue re-renders on each
 * /gt/queue response.
 */
(function () {
  let sessionsTimer = null;
  let queueTimer = null;

  async function tickSessions() {
    try {
      const r = await fetch('/gt/sessions');
      if (!r.ok) return;
      const data = await r.json();
      window.GT.sessions = data.sessions || [];
      if (window.GT.render.sessionList) window.GT.render.sessionList();
      if (window.GT.render.editor) window.GT.render.editor();
    } catch (e) {
      // network blip — keep stale state
    }
  }

  async function tickQueue() {
    try {
      const r = await fetch('/gt/queue');
      if (!r.ok) return;
      const data = await r.json();
      window.GT.queue = data;
      if (window.GT.render.queue) window.GT.render.queue();
    } catch (e) {
      // network blip — keep stale state
    }
  }

  window.GT.tickSessions = tickSessions;
  window.GT.tickQueue = tickQueue;

  document.addEventListener('DOMContentLoaded', () => {
    if (window.GT.render.sessionList) window.GT.render.sessionList();
    if (window.GT.render.queue) window.GT.render.queue();
    if (window.GT.render.editor) window.GT.render.editor();

    sessionsTimer = setInterval(tickSessions, 5000);
    queueTimer = setInterval(tickQueue, 1000);
  });
})();
