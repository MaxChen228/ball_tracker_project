  // Phase A picker cascade: when the algorithm dropdown changes, hide
  // preset options whose `data-algo` doesn't match. Preset options
  // themselves are server-rendered into the form; this is pure DOM
  // filtering, no fetch. Wrapped in try/catch because every viewer JS
  // file lives in the shared 00_boot.js IIFE — a top-level throw here
  // would abort all subsequent files (setInterval / event handlers /
  // SSE plumbing) and the whole page degrades to "click works once
  // then nothing updates" without an obvious symptom.
  try {
    const pickerForm = document.querySelector("form[data-algorithm-picker]");
    if (pickerForm) {
      const algoSel = pickerForm.querySelector('select[name="algorithm_id"]');
      const presetSel = pickerForm.querySelector('select[name="preset_name"]');
      if (algoSel && presetSel) {
        const applyAlgorithmFilter = () => {
          const algo = algoSel.value;
          let firstVisible = null;
          let currentStillVisible = false;
          for (const opt of presetSel.options) {
            const matches = opt.dataset.algo === algo;
            opt.hidden = !matches;
            opt.disabled = !matches;
            if (matches) {
              if (firstVisible === null) firstVisible = opt;
              if (opt.selected) currentStillVisible = true;
            }
          }
          // Server's default `selected` may not match the algorithm
          // dropdown's initial value (e.g. session active points at an
          // algorithm with no preset on disk). Snap to the first
          // visible option so submission is never on a hidden option.
          if (!currentStillVisible) {
            presetSel.value = firstVisible ? firstVisible.value : "";
          }
        };
        algoSel.addEventListener("change", applyAlgorithmFilter);
        applyAlgorithmFilter();
      }
    }
  } catch (err) {
    console.warn("75_algorithm_picker init failed:", err);
  }
