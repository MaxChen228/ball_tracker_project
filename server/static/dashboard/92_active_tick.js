// === tickActiveSession (retired) ===

  // The 10 Hz active-session card tick was retired alongside the
  // Session Monitor card. The function is kept as a no-op stub so
  // downstream callers (if any) don't ReferenceError; the
  // setInterval(tickActiveSession, 100) in 94_main.js has been
  // removed.
  function tickActiveSession() { /* no-op */ }
