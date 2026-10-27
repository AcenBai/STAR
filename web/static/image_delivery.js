/* Image transport only. No answer, registration, current-task or navigation API. */
class ImageDelivery {
  constructor({ getTask, getUserId, getImage, setImage, setStatus }) {
    Object.assign(this, { getTask, getUserId, getImage, setImage, setStatus });
    this.current = null;
    this.prefetch = null;
    this.prefetchTimer = null;
    this.sequence = 0;
    this.pageId = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    globalThis.addEventListener?.("pagehide", () => this.clear());
  }

  now() { return globalThis.performance?.now?.() ?? Date.now(); }

  resourceTiming(watch) {
    try {
      const entries = globalThis.performance?.getEntriesByName?.(watch.url) || [];
      const entry = entries.filter(item => item.entryType === "resource"
        && item.startTime >= watch.networkStarted - 1).at(-1);
      if (!entry) return undefined;
      return {
        duration_ms: entry.duration,
        ttfb_ms: Math.max(0, entry.responseStart - entry.requestStart),
        download_ms: Math.max(0, entry.responseEnd - entry.responseStart),
        transfer_bytes: entry.transferSize,
        encoded_bytes: entry.encodedBodySize,
        decoded_bytes: entry.decodedBodySize,
        protocol: entry.nextHopProtocol || "",
      };
    } catch (_) { return undefined; }
  }

  report(watch, outcome, elapsed = this.now() - watch.started) {
    if (!watch.userId) return;
    const payload = {
      version: 1, request_id: watch.id, user_id: watch.userId,
      image_id: watch.imageId, kind: watch.kind, outcome,
      client_time: new Date().toISOString(),
      elapsed_ms: Math.max(0, elapsed),
      prefetch_used: watch.prefetchUsed,
      resource: this.resourceTiming(watch),
    };
    try {
      const body = JSON.stringify(payload);
      if (globalThis.navigator?.sendBeacon?.("/api/image-metrics",
        new Blob([body], { type: "application/json" }))) return;
      Promise.resolve(fetch("/api/image-metrics", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body, keepalive: true,
      })).catch(() => {});
    } catch (_) { /* Diagnostic failures never reach the answer form. */ }
  }

  watch(image, imageId, url, kind, prefetched = null) {
    const watch = {
      id: `${this.pageId}-${++this.sequence}`, imageId, url, kind,
      userId: this.getUserId(), started: this.now(), settled: false,
      networkStarted: prefetched?.watch.networkStarted ?? this.now(),
      prefetchUsed: Boolean(prefetched),
    };
    watch.timer = setTimeout(() => {
      if (!watch.settled) this.report(watch, "slow");
    }, 15000);
    image.addEventListener("load", () => this.finish(watch, "loaded"));
    image.addEventListener("error", () => this.finish(watch, "error"));
    return watch;
  }

  finish(watch, outcome) {
    if (!watch || watch.settled) return;
    watch.settled = true;
    clearTimeout(watch.timer);
    // Resource entries are available after the image's load event dispatch.
    const elapsed = this.now() - watch.started;
    queueMicrotask(() => this.report(watch, outcome, elapsed));
  }

  cancelPrefetch() {
    clearTimeout(this.prefetchTimer);
    this.prefetchTimer = null;
    const pending = this.prefetch;
    this.prefetch = null;
    if (pending) {
      this.finish(pending.watch, "cancelled");
      pending.image.removeAttribute("src");
    }
  }

  clear() {
    this.cancelPrefetch();
    const current = this.current;
    this.current = null;
    if (current) {
      this.finish(current.watch, "cancelled");
      this.finish(current.prefetchWatch, "cancelled");
      current.image.removeAttribute("src");
    }
  }

  queueNext(current) {
    clearTimeout(this.prefetchTimer);
    this.prefetchTimer = setTimeout(() => {
      this.prefetchTimer = null;
      const task = this.getTask();
      if (this.current !== current || !current.loaded || task?.done
          || task?.image_id !== current.imageId || current.userId !== this.getUserId()) return;
      const next = task.next_image;
      if (!next?.image_id || next.image_id === current.imageId || !next.image_url) return;
      const url = new URL(next.image_url, window.location.href);
      const origin = new URL(window.location.href).origin;
      if (url.origin !== origin || url.pathname !== `/image/${next.image_id}` || url.search) return;
      if (this.prefetch?.imageId === next.image_id && this.prefetch.userId === current.userId) return;
      this.cancelPrefetch();
      const image = document.createElement("img");
      image.fetchPriority = "low";
      const pending = { image, imageId: next.image_id, url: url.href,
        userId: current.userId, loaded: false, failed: false };
      this.prefetch = pending;
      pending.watch = this.watch(image, pending.imageId, pending.url, "prefetch");
      image.addEventListener("load", () => { pending.loaded = true; });
      image.addEventListener("error", () => { pending.failed = true; });
      image.src = pending.url;
      // No load callback starts another prefetch: this is exactly one lookahead.
    }, 250);
  }

  show(task, reload = false) {
    const userId = this.getUserId();
    const candidate = !reload && this.prefetch?.imageId === task.image_id
      && this.prefetch.userId === userId && !this.prefetch.failed ? this.prefetch : null;
    if (candidate) this.prefetch = null;
    this.cancelPrefetch();
    const previous = this.current;
    this.current = null;
    if (previous) {
      this.finish(previous.watch, "cancelled");
      this.finish(previous.prefetchWatch, "cancelled");
    }
    const oldImage = this.getImage();
    const image = candidate?.image || document.createElement("img");
    image.id = "taskImage";
    image.alt = oldImage.alt;
    image.fetchPriority = "high";
    const url = new URL(task.image_url, window.location.href);
    if (reload) url.searchParams.set("reload", `${Date.now()}-${++this.sequence}`);
    const current = { image, imageId: task.image_id, userId,
      loaded: Boolean(candidate?.loaded), prefetchWatch: candidate?.watch };
    this.current = current;
    current.watch = this.watch(image, current.imageId, url.href,
      reload ? "reload" : "display", candidate);
    const active = () => this.current === current && this.getTask()?.image_id === current.imageId
      && this.getUserId() === current.userId;
    image.addEventListener("load", () => {
      if (!active()) return;
      current.loaded = true;
      this.setStatus("Loaded");
      this.queueNext(current);
    });
    image.addEventListener("error", () => {
      if (active()) this.setStatus("Load failed. Retry.", true);
    });
    this.setImage(image);
    oldImage.removeAttribute("src");
    this.setStatus(reload ? "Reloading…" : "Loading…");
    if (current.loaded) {
      this.finish(current.watch, "loaded");
      this.setStatus("Loaded");
      this.queueNext(current);
    } else if (!candidate) {
      image.src = url.href;
    }
  }
}
