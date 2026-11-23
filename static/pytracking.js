/* SPDX-License-Identifier: Apache-2.0 */
/*!
 * pytracking browser SDK - Phase 2.
 *
 * Vanilla JS, no dependencies, no build step. Posts events to the receiver at
 * POST {url}/api/v2/ev. The write key travels only in the `wk` field.
 *
 * Usage:
 *   pytracking.init({ url: "https://my.herbsworld.net/api/v2/ev", writeKey: "..." });
 *   pytracking.purchase({ value: 49.99, currency: "USD", email: "a@b.c" });
 *
 * Email and phone are SHA-256 hashed in the browser before leaving the page,
 * so raw PII is never transmitted. They are skipped when crypto.subtle is
 * unavailable (e.g. a plain-http page outside localhost).
 *
 * Bodies are sent as text/plain on purpose: that is a CORS "simple request",
 * so no preflight round trip is needed, and it is what sendBeacon gives us.
 * The receiver parses the raw body as JSON rather than trusting Content-Type.
 */
(function (window, document) {
  "use strict";

  var MAX_BODY = 16000; /* receiver rejects over 16384 bytes */
  var config = {
    url: "",
    writeKey: "",
    debug: false,
    consent: null,
    autoPageView: true
  };
  var queue = [];

  function log() {
    if (config.debug && window.console && console.log) {
      console.log.apply(console, ["[pytracking]"].concat([].slice.call(arguments)));
    }
  }

  function uuid() {
    if (window.crypto && crypto.randomUUID) {
      try {
        return crypto.randomUUID().replace(/-/g, "");
      } catch (e) {}
    }
    var out = "";
    for (var i = 0; i < 32; i++) {
      out += Math.floor(Math.random() * 16).toString(16);
    }
    return out;
  }

  function cookie(name) {
    if (!document.cookie) return undefined;
    var match = document.cookie.match(
      new RegExp("(?:^|; )" + name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "=([^;]*)")
    );
    if (!match) return undefined;
    try {
      return decodeURIComponent(match[1]);
    } catch (e) {
      return match[1];
    }
  }

  function clip(value, max) {
    if (value === undefined || value === null || value === "") return undefined;
    var text = String(value);
    return text.length > max ? text.slice(0, max) : text;
  }

  function normalizeEmail(value) {
    return String(value).trim().toLowerCase();
  }

  function normalizePhone(value) {
    var raw = String(value).trim();
    var digits = raw.replace(/[^0-9]/g, "");
    if (!digits) return "";
    return (raw.charAt(0) === "+" ? "+" : "") + digits;
  }

  function sha256(value) {
    var subtle = window.crypto && window.crypto.subtle;
    if (!subtle || !window.TextEncoder) {
      log("crypto.subtle unavailable; skipping hash");
      return Promise.resolve(undefined);
    }
    var bytes = new TextEncoder().encode(value);
    return subtle.digest("SHA-256", bytes).then(
      function (buffer) {
        var view = new Uint8Array(buffer);
        var hex = "";
        for (var i = 0; i < view.length; i++) {
          hex += ("0" + view[i].toString(16)).slice(-2);
        }
        return hex;
      },
      function () {
        return undefined;
      }
    );
  }

  function basePayload(name, props) {
    props = props || {};
    var userData = {
      fbp: cookie("_fbp"),
      fbc: cookie("_fbc"),
      client_user_agent: clip(
        window.navigator && window.navigator.userAgent, 255)
    };
    var custom = {};
    for (var key in props) {
      if (!Object.prototype.hasOwnProperty.call(props, key)) continue;
      custom[key] = props[key];
    }
    delete custom.email;
    delete custom.em;
    delete custom.phone;
    delete custom.ph;
    delete custom.event_id;

    return {
      payload: {
        event_id: clip(props.event_id, 64) || uuid(),
        name: name,
        event_time: Math.floor(Date.now() / 1000),
        user_data: userData,
        custom_data: custom,
        source: {
          url: clip(window.location && window.location.href, 500),
          referrer: clip(document.referrer, 500)
        }
      },
      email: props.email || props.em,
      phone: props.phone || props.ph
    };
  }

  function removeItem(item) {
    var index = queue.indexOf(item);
    if (index !== -1) queue.splice(index, 1);
  }

  function beacon(item) {
    var navigator = window.navigator;
    if (!navigator || !navigator.sendBeacon) return false;
    var body = item.body;
    try {
      if (navigator.sendBeacon(config.url, new Blob([body], { type: "text/plain;charset=UTF-8" }))) {
        removeItem(item);
        return true;
      }
    } catch (e) {}
    try {
      if (navigator.sendBeacon(config.url, body)) {
        removeItem(item);
        return true;
      }
    } catch (e) {}
    return false;
  }

  function deliver(item) {
    if (!window.fetch) {
      beacon(item);
      return;
    }
    try {
      fetch(config.url, {
        method: "POST",
        body: item.body,
        keepalive: true,
        headers: { "Content-Type": "text/plain;charset=UTF-8" }
      }).then(
        function () {
          removeItem(item);
          log("sent", item.body.length, "bytes");
        },
        function (error) {
          log("send failed", error && error.message);
        }
      );
    } catch (error) {
      log("fetch threw", error && error.message);
    }
  }

  function flushBeacons() {
    var pending = queue.slice();
    for (var i = 0; i < pending.length; i++) {
      if (!beacon(pending[i])) deliver(pending[i]);
    }
  }

  function enqueue(payload) {
    var body = JSON.stringify(payload);
    if (body.length > MAX_BODY) {
      log("event dropped: body would exceed the receiver limit", body.length);
      return;
    }
    var item = { body: body };
    queue.push(item);
    if (document.visibilityState !== "hidden") deliver(item);
  }

  function track(name, props) {
    if (!config.url || !config.writeKey) {
      log("init() must be called before track()");
      return;
    }
    if (typeof name !== "string" || !name) return;

    var built = basePayload(name, props);
    var payload = built.payload;
    if (config.consent !== null && config.consent !== undefined) {
      payload.consent_state = config.consent;
    }
    payload.wk = config.writeKey;

    var jobs = [];
    if (built.email) {
      jobs.push(
        sha256(normalizeEmail(built.email)).then(function (hash) {
          if (hash) payload.user_data.em = hash;
        })
      );
    }
    if (built.phone) {
      jobs.push(
        sha256(normalizePhone(built.phone)).then(function (hash) {
          if (hash) payload.user_data.ph = hash;
        })
      );
    }

    if (!jobs.length) {
      enqueue(payload);
      return;
    }
    Promise.all(jobs).then(function () {
      enqueue(payload);
    });
  }

  var api = {
    init: function (options) {
      options = options || {};
      if (options.url) config.url = String(options.url);
      if (options.writeKey) config.writeKey = String(options.writeKey);
      config.debug = !!options.debug;
      config.autoPageView = options.autoPageView !== false;
      config.consent =
        options.consent === undefined ? null : options.consent;
      config._ready = !!(config.url && config.writeKey);
      log("initialized", config.url, "autoPageView=" + config.autoPageView);
      if (config._ready && config.autoPageView) {
        track("page_view", options.pageProps || {});
      }
      return api;
    },
    track: track,
    pageView: function (props) {
      return track("page_view", props);
    },
    addToCart: function (props) {
      return track("add_to_cart", props);
    },
    checkout: function (props) {
      return track("checkout", props);
    },
    purchase: function (props) {
      return track("purchase", props);
    },
    config: config
  };

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") flushBeacons();
  });
  window.addEventListener("pagehide", flushBeacons);

  window.pytracking = api;
})(window, document);
