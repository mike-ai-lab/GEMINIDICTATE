(() => {
  if (window.__uvpInitialized) return;
  window.__uvpInitialized = true;

  const log = (...a) => console.log("[UVP]", ...a);
  const warn = (...a) => console.warn("[UVP]", ...a);
  log("Content script loaded on", location.href, "| version: 5");

  let enabled = false;
  const active = new Map(); // host -> { cleanup }

  const EXTENSIONS = /\.(mp4|webm|ogv|ogg|m4v|m3u8)(?:[?#].*)?$/i;

  // --- Resolve preview target ---
  function resolvePreviewTarget(el) {
    let node = el;
    for (let i = 0; i < 10 && node && node !== document.body && node !== document.documentElement; i++, node = node.parentElement) {
      if (node.tagName === "A") {
        const fallback = node.getAttribute("data-previewvideo-fallback");
        const primary  = node.getAttribute("data-previewvideo");
        const src = fallback || primary;
        if (src && EXTENSIONS.test(src)) {
          try {
            const img = node.querySelector("img");
            return { host: node, src: new URL(src, location.href).href, img, isNativeVideo: false };
          } catch {}
        }
        const href = node.href || "";
        if (EXTENSIONS.test(href) && /^https?:/i.test(href)) {
          return { host: node, src: href, img: node.querySelector("img"), isNativeVideo: false };
        }
        for (const attr of ["data-video", "data-video-src", "data-mp4", "data-src"]) {
          const val = node.getAttribute(attr);
          if (val && EXTENSIONS.test(val)) {
            try { return { host: node, src: new URL(val, location.href).href, img: node.querySelector("img"), isNativeVideo: false }; } catch {}
          }
        }
      }

      // non-anchor data-previewvideo
      const fb = node.getAttribute("data-previewvideo-fallback");
      const pr = node.getAttribute("data-previewvideo");
      const ps = fb || pr;
      if (ps && EXTENSIONS.test(ps)) {
        try { return { host: node, src: new URL(ps, location.href).href, img: node.querySelector("img"), isNativeVideo: false }; } catch {}
      }

      for (const attr of ["data-video", "data-video-src", "data-mp4"]) {
        const val = node.getAttribute(attr);
        if (val && EXTENSIONS.test(val)) {
          try { return { host: node, src: new URL(val, location.href).href, img: node.querySelector("img"), isNativeVideo: false }; } catch {}
        }
      }

      if (node.tagName === "VIDEO") {
        const src = node.currentSrc || node.src || node.querySelector("source")?.src || "";
        if (/^https?:/i.test(src)) {
          // Skip if this video is a main page player — check if it's thumbnail-sized
          const r = node.getBoundingClientRect();
          if (r.width > 600 || r.height > 400) {
            return null; // too large — it's a main player, not a thumbnail
          }
          // Also skip if inside a known player wrapper
          const playerSelector = '.xplayer, .video-player, [class*="player"], [id*="player"], [class*="Player"], [id*="Player"]';
          if (node.closest(playerSelector)) return null;
          return { host: node, src: null, img: null, isNativeVideo: true };
        }
        return null; // blob = DRM player, skip
      }

      const nested = node.querySelector("video");
      if (nested) {
        const src = nested.currentSrc || nested.src || nested.querySelector("source")?.src || "";
        if (/^https?:/i.test(src)) {
          const r = nested.getBoundingClientRect();
          if (r.width > 600 || r.height > 400) return null;
          if (nested.closest(".xplayer, .video-player, [class*='player'], [id*='player'], [class*='Player'], [id*='Player']")) return null;
          return { host: nested, src: null, img: null, isNativeVideo: true };
        }
        return null; // blob nested = DRM, skip entire container
      }
    }
    return null;
  }

  // --- Start preview ---
  function startPreview(target) {
    const { host, src, img, isNativeVideo } = target;
    if (active.has(host)) return;

    if (isNativeVideo) {
      log("startPreview: native video", host.currentSrc || host.src);
      const orig = { muted: host.muted, volume: host.volume, loop: host.loop, controls: host.controls };
      host.muted = true; host.defaultMuted = true; host.volume = 0;
      host.setAttribute("muted", ""); host.loop = true; host.controls = false;
      if (!host.currentSrc && !host.src && !host.querySelector("source")) { warn("no src"); return; }
      const tryPlay = () => host.play().then(() => log("native playing")).catch(e => warn("play failed", e.name, e.message));
      if (host.readyState === 0) { host.load(); host.addEventListener("canplay", tryPlay, { once: true }); }
      else tryPlay();

      active.set(host, {
        cleanup() {
          host.pause();
          host.muted = orig.muted; host.volume = orig.volume;
          host.loop = orig.loop; host.controls = orig.controls;
          if (!orig.muted) host.removeAttribute("muted");
        }
      });

    } else {
      const refEl = img || host;
      const rect = refEl.getBoundingClientRect();
      if (rect.width < 40 || rect.height < 30) { warn("too small"); return; }
      log("startPreview: overlay", Math.round(rect.width), "x", Math.round(rect.height), src);

      const video = document.createElement("video");
      video.muted = true; video.defaultMuted = true; video.volume = 0;
      video.autoplay = true; video.loop = true; video.playsInline = true; video.preload = "auto";
      video.setAttribute("muted", ""); video.setAttribute("playsinline", "");
      video.dataset.uvpOverlay = "1";
      video.src = src;

      // Use a shadow host to escape any stacking context on the page
      const shadow = document.createElement("div");
      Object.assign(shadow.style, {
        position: "fixed",
        top: "0", left: "0",
        width: "0", height: "0",
        overflow: "visible",
        zIndex: "2147483647",
        pointerEvents: "none",
        margin: "0", padding: "0",
        border: "none", background: "none",
        transform: "none", filter: "none",
        isolation: "isolate",
      });

      Object.assign(video.style, {
        position: "fixed",
        top: rect.top + "px", left: rect.left + "px",
        width: rect.width + "px", height: rect.height + "px",
        objectFit: "cover", zIndex: "2147483647",
        borderRadius: getComputedStyle(refEl).borderRadius || "0",
        pointerEvents: "none", background: "#111",
        margin: "0", padding: "0", display: "block",
      });

      shadow.appendChild(video);
      document.documentElement.appendChild(shadow);
      let removed = false;

      const reposition = () => {
        const nr = refEl.getBoundingClientRect();
        video.style.top = nr.top + "px"; video.style.left = nr.left + "px";
        video.style.width = nr.width + "px"; video.style.height = nr.height + "px";
      };
      window.addEventListener("scroll", reposition, { passive: true });
      window.addEventListener("resize", reposition, { passive: true });

      const tryPlay = () => {
        if (removed) return;
        video.play().then(() => { if (!removed) log("overlay playing"); }).catch(e => { if (!removed) warn("overlay play failed", e.name); });
      };
      if (video.readyState >= 2) tryPlay();
      else video.addEventListener("canplay", tryPlay, { once: true });

      // Hover starts the preview — it keeps playing until extension is disabled
      active.set(host, {
        cleanup() {
          removed = true;
          video.pause(); video.remove();
          shadow.remove();
          window.removeEventListener("scroll", reposition);
          window.removeEventListener("resize", reposition);
        }
      });
    }

    log("active:", active.size);
  }

  function stopPreview(host) {
    const entry = active.get(host);
    if (!entry) return;
    entry.cleanup();
    active.delete(host);
    log("stopped, active:", active.size);
  }

  // mouseover TOGGLES preview — first hover starts, second hover stops
  function onMouseOver(e) {
    if (!enabled) return;
    const target = resolvePreviewTarget(e.target);
    if (!target) return;
    const { host } = target;
    if (active.has(host)) {
      stopPreview(host);
    } else {
      startPreview(target);
    }
  }

  function attachListeners() { document.addEventListener("mouseover", onMouseOver, true); log("Listeners attached"); }
  function detachListeners() { document.removeEventListener("mouseover", onMouseOver, true); log("Listeners detached"); }

  function setEnabled(value) {
    enabled = !!value;
    log("setEnabled →", enabled);
    if (enabled) { attachListeners(); }
    else { detachListeners(); for (const [host] of active) stopPreview(host); active.clear(); }
    return { enabled, count: active.size };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === "UVP_GET_STATE") sendResponse({ enabled, count: active.size });
    if (message?.type === "UVP_SET_ENABLED") sendResponse(setEnabled(message.enabled));
    return true;
  });

  attachListeners();
  log("Initialized. enabled =", enabled);
})();
