"use strict";

// WebCodecs frame source. One MP4 download → mp4box demux → VideoDecoder
// feeds samples in DTS order → output callback emits VideoFrame → we tonemap
// to ImageBitmap (preview res) and cache by frame index.
//
// Random seek = forward decode from nearest preceding sync sample. iPhone
// 240fps source has GOP≈29, so worst-case cold seek ≈ 29 chunks ≈ 50ms on
// hardware decoder. Sequential next-frame just submits one more delta sample
// without re-keyframing.
//
// CRITICAL invariants:
//   - Every VideoFrame emitted by decoder MUST be .close()'d (GPU mem leak).
//   - The bitmap cache OWNS its ImageBitmaps; callers must not close them.
//   - All chunks must be submitted to the decoder in DTS order.

const PREVIEW_WIDTH = 640;
const BITMAP_CACHE_MAX = 240;   // ~700KB each at 640x360 RGBA → ~165MB

class FrameSource {
  constructor() {
    this._samples = null;            // [{data, isSync, ptsUs, dts, frameIdx}], display order
    this._ptsToIdx = null;           // Map<ptsUs, frameIdx>
    this._decoder = null;
    this._cache = new Map();         // Map<frameIdx, ImageBitmap>, LRU
    this._pending = new Map();       // Map<frameIdx, [{resolve,reject}, ...]>
    this._runKey = -1;               // sync sample idx that started current decode run
    this._runTail = -1;              // last frame idx submitted in current run
    this._closed = false;
    this.width = 0;
    this.height = 0;
    this.previewWidth = 0;
    this.previewHeight = 0;
  }

  async load(url) {
    if (typeof MP4Box === "undefined") throw new Error("mp4box.js not loaded");
    if (typeof VideoDecoder === "undefined") throw new Error("WebCodecs unsupported");

    const buf = await (await fetch(url)).arrayBuffer();
    const mp4 = MP4Box.createFile();

    let info;
    let resolveSamples;
    const samplesP = new Promise((r) => (resolveSamples = r));
    const collected = [];

    mp4.onError = (e) => { throw new Error("mp4box: " + e); };
    const readyP = new Promise((resolve) => {
      mp4.onReady = (i) => { info = i; resolve(); };
    });
    mp4.onSamples = (id, _user, batch) => {
      for (const s of batch) collected.push(s);
      const track = info && info.videoTracks[0];
      if (track && collected.length >= track.nb_samples) resolveSamples();
    };

    buf.fileStart = 0;
    mp4.appendBuffer(buf);
    mp4.flush();
    await readyP;

    const track = info.videoTracks[0];
    if (!track) throw new Error("no video track");
    this.width = track.video.width;
    this.height = track.video.height;
    const aspect = this.width / this.height;
    this.previewWidth = Math.min(PREVIEW_WIDTH, this.width);
    this.previewHeight = Math.round(this.previewWidth / aspect);

    const description = this._extractCodecDescription(mp4, track);
    const cfg = {
      codec: track.codec,
      codedWidth: this.width,
      codedHeight: this.height,
      description,
      optimizeForLatency: true,
    };
    const support = await VideoDecoder.isConfigSupported(cfg);
    if (!support.supported) throw new Error(`codec not supported: ${track.codec}`);

    this._decoder = new VideoDecoder({
      output: (vf) => this._onFrame(vf),
      error: (e) => console.error("VideoDecoder error", e),
    });
    this._decoder.configure(cfg);

    mp4.setExtractionOptions(track.id, null, { nbSamples: track.nb_samples });
    mp4.start();
    // Force flush in case all samples are already buffered (synchronous case).
    if (collected.length >= track.nb_samples) resolveSamples();
    await Promise.race([
      samplesP,
      new Promise((_, rej) => setTimeout(() => rej(new Error("sample extraction timeout")), 15000)),
    ]);

    // Display order = sort by composition timestamp. The i-th entry IS frame i.
    collected.sort((a, b) => a.cts - b.cts);
    this._samples = collected.map((s, i) => ({
      data: s.data,
      isSync: !!s.is_sync,
      ptsUs: Math.round((s.cts / s.timescale) * 1e6),
      dts: s.dts,
      frameIdx: i,
    }));
    this._ptsToIdx = new Map();
    for (const s of this._samples) this._ptsToIdx.set(s.ptsUs, s.frameIdx);
  }

  _extractCodecDescription(mp4, track) {
    const trak = mp4.getTrackById(track.id);
    for (const entry of trak.mdia.minf.stbl.stsd.entries) {
      const box = entry.avcC || entry.hvcC || entry.vpcC || entry.av1C;
      if (!box) continue;
      // mp4box exposes DataStream as a top-level global, not as MP4Box.DataStream.
      const stream = new DataStream(undefined, 0, DataStream.BIG_ENDIAN);
      box.write(stream);
      // Trim 8-byte ISO box header (size+type) → raw avcC payload.
      return new Uint8Array(stream.buffer, 8);
    }
    throw new Error("no codec config box (avcC/hvcC/vpcC/av1C) on track");
  }

  // Public: ask for a frame. Resolves with cached ImageBitmap; multiple awaits
  // on the same idx coalesce into one decode.
  getFrame(frameIdx) {
    if (this._closed) return Promise.reject(new Error("FrameSource closed"));
    if (!this._samples) return Promise.reject(new Error("FrameSource not loaded"));
    if (frameIdx < 0 || frameIdx >= this._samples.length) {
      return Promise.reject(new Error("frame out of range: " + frameIdx));
    }
    const cached = this._cache.get(frameIdx);
    if (cached) {
      this._cache.delete(frameIdx);
      this._cache.set(frameIdx, cached);
      return Promise.resolve(cached);
    }
    return new Promise((resolve, reject) => {
      const arr = this._pending.get(frameIdx);
      if (arr) { arr.push({ resolve, reject }); return; }
      this._pending.set(frameIdx, [{ resolve, reject }]);
      this._scheduleDecodeFor(frameIdx);
    });
  }

  // Synchronous cache lookup; null if miss. Used by paint code that wants to
  // know without committing to await.
  peek(frameIdx) {
    const bm = this._cache.get(frameIdx);
    if (!bm) return null;
    this._cache.delete(frameIdx);
    this._cache.set(frameIdx, bm);
    return bm;
  }

  // Fire-and-forget bulk decode of a contiguous range. Failures are silent
  // (those frames just stay un-cached; explicit getFrame later will retry).
  prefetchRange(lo, hi) {
    lo = Math.max(0, lo);
    hi = Math.min(this._samples.length - 1, hi);
    for (let f = lo; f <= hi; f++) {
      if (this._cache.has(f) || this._pending.has(f)) continue;
      this.getFrame(f).catch(() => {});
    }
  }

  _scheduleDecodeFor(frameIdx) {
    // Find the GOP containing frameIdx: [gopStart..gopEnd) where gopStart is
    // the sync sample at or before frameIdx, and gopEnd is the next sync (or
    // end of stream). We submit the WHOLE GOP every time — never a partial
    // slice — because B-frames inside the GOP forward-reference later I/P
    // frames in display order. A truncated slice [syncIdx..frameIdx] would
    // leave B-frames decoding without their forward reference, producing
    // chunky/blocky artifacts on motion regions.
    let gopStart = frameIdx;
    while (gopStart > 0 && !this._samples[gopStart].isSync) gopStart--;
    let gopEnd = frameIdx + 1;
    while (gopEnd < this._samples.length && !this._samples[gopEnd].isSync) gopEnd++;

    if (this._runKey === gopStart && this._runTail >= gopEnd - 1) {
      // Entire GOP already submitted; decoder will emit any frame in it.
      return;
    }

    // Submit the GOP in DTS (decode) order. Decoder treats the leading key
    // sample as IDR → flushes prior reference state automatically.
    const span = this._samples.slice(gopStart, gopEnd);
    span.sort((a, b) => a.dts - b.dts);
    for (const s of span) {
      this._decoder.decode(new EncodedVideoChunk({
        type: s.isSync ? "key" : "delta",
        timestamp: s.ptsUs,
        data: s.data,
      }));
    }
    this._runKey = gopStart;
    this._runTail = gopEnd - 1;
  }

  async _onFrame(vf) {
    const frameIdx = this._ptsToIdx ? this._ptsToIdx.get(vf.timestamp) : null;
    if (frameIdx == null) { vf.close(); return; }
    let bm;
    try {
      bm = await createImageBitmap(vf, {
        resizeWidth: this.previewWidth,
        resizeHeight: this.previewHeight,
        resizeQuality: "medium",
      });
    } catch (e) {
      vf.close();
      const arr = this._pending.get(frameIdx);
      if (arr) {
        for (const p of arr) p.reject(e);
        this._pending.delete(frameIdx);
      }
      return;
    }
    vf.close();
    if (this._closed) { bm.close(); return; }

    const existing = this._cache.get(frameIdx);
    if (existing) {
      // Came in twice (e.g. a re-issue raced with the original emit). Drop the new one.
      bm.close();
    } else {
      this._cache.set(frameIdx, bm);
      while (this._cache.size > BITMAP_CACHE_MAX) {
        const oldest = this._cache.keys().next().value;
        const old = this._cache.get(oldest);
        this._cache.delete(oldest);
        if (old && typeof old.close === "function") old.close();
      }
    }
    const final = this._cache.get(frameIdx);
    const arr = this._pending.get(frameIdx);
    if (arr) {
      for (const p of arr) p.resolve(final);
      this._pending.delete(frameIdx);
    }
  }

  close() {
    this._closed = true;
    for (const [, arr] of this._pending) {
      for (const p of arr) p.reject(new Error("FrameSource closed"));
    }
    this._pending.clear();
    for (const bm of this._cache.values()) {
      if (bm && typeof bm.close === "function") bm.close();
    }
    this._cache.clear();
    if (this._decoder) {
      try { this._decoder.close(); } catch (_) {}
      this._decoder = null;
    }
  }
}

window.FrameSource = FrameSource;
