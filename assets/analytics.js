/*
 * Tracking del smoke test.
 * Expone window.track(eventName, props) que SIEMPRE:
 *   1. Escribe en la consola.
 *   2. Guarda el evento en localStorage (clave "st_events").
 * Y opcionalmente (si hay claves en config.js):
 *   3. Lo envía a PostHog.
 *   4. Lo envía por sendBeacon a beaconUrl (webhook.site).
 */
(function () {
  var CFG = (window.SMOKE_CONFIG || {});
  var STORE_KEY = "st_events";

  // --- Identificador anónimo y estable por navegador (sin login, sin PII) ---
  function getAnonId() {
    try {
      var id = localStorage.getItem("st_anon_id");
      if (!id) {
        id = "anon_" + Math.random().toString(36).slice(2) + Date.now().toString(36);
        localStorage.setItem("st_anon_id", id);
      }
      return id;
    } catch (e) {
      return "anon_unknown";
    }
  }

  // --- PostHog (solo si hay clave) ---
  function initPostHog() {
    if (!CFG.posthogKey) return;
    /* snippet oficial de PostHog (cargado solo cuando hay clave) */
    !function(t,e){var o,n,p,r;e.__SV||(window.posthog=e,e._i=[],e.init=function(i,s,a){function g(t,e){var o=e.split(".");2==o.length&&(t=t[o[0]],e=o[1]),t[e]=function(){t.push([e].concat(Array.prototype.slice.call(arguments,0)))}}(p=t.createElement("script")).type="text/javascript",p.async=!0,p.src=s.api_host+"/static/array.js",(r=t.getElementsByTagName("script")[0]).parentNode.insertBefore(p,r);var u=e;for(void 0!==a?u=e[a]=[]:a="posthog",u.people=u.people||[],u.toString=function(t){var e="posthog";return"posthog"!==a&&(e+="."+a),t||(e+=" (stub)"),e},u.people.toString=function(){return u.toString(1)+".people (stub)"},o="capture identify alias people.set people.set_once set_config register register_once unregister opt_out_capturing has_opted_out_capturing opt_in_capturing reset isFeatureEnabled onFeatureFlags getFeatureFlag getFeatureFlagPayload reloadFeatureFlags group updateEarlyAccessFeatureEnrollment getEarlyAccessFeatures getActiveMatchingSurveys getSurveys getNextSurveyStep onSessionId".split(" "),n=0;n<o.length;n++)g(u,o[n]);e._i.push([i,s,a])},e.__SV=1)}(document,window.posthog||[]);
    window.posthog.init(CFG.posthogKey, { api_host: CFG.posthogHost || "https://us.i.posthog.com" });
  }

  function saveLocal(evt) {
    try {
      var arr = JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
      arr.push(evt);
      localStorage.setItem(STORE_KEY, JSON.stringify(arr));
    } catch (e) { /* localStorage no disponible: ignorar */ }
  }

  window.track = function (eventName, props) {
    var evt = Object.assign(
      {
        event: eventName,
        ts: new Date().toISOString(),
        anonId: getAnonId(),
        path: location.pathname + location.search
      },
      props || {}
    );

    // 1 + 2: consola + localStorage (siempre)
    console.log("[track]", eventName, evt);
    saveLocal(evt);

    // 3: PostHog (opcional)
    if (CFG.posthogKey && window.posthog && window.posthog.capture) {
      window.posthog.capture(eventName, evt);
    }

    // 4: beacon (opcional)
    if (CFG.beaconUrl && navigator.sendBeacon) {
      try {
        var blob = new Blob([JSON.stringify(evt)], { type: "application/json" });
        navigator.sendBeacon(CFG.beaconUrl, blob);
      } catch (e) { /* ignorar */ }
    }
  };

  // Util para depurar: imprime los eventos guardados.
  window.dumpEvents = function () {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || "[]"); }
    catch (e) { return []; }
  };

  // Cablear clics en CTAs de forma declarativa: cualquier elemento con
  // data-track="cta_click" dispara el evento (con data-* extra como props).
  function wireDataTracks() {
    document.addEventListener("click", function (e) {
      var el = e.target.closest("[data-track]");
      if (!el) return;
      var name = el.getAttribute("data-track");
      var props = {};
      Object.keys(el.dataset).forEach(function (k) {
        if (k !== "track") props[k] = el.dataset[k];
      });
      window.track(name, props);
    });
  }

  // Inicialización al cargar.
  initPostHog();
  document.addEventListener("DOMContentLoaded", function () {
    wireDataTracks();
    // page_view automático en cada página.
    window.track("page_view", { title: document.title });
  });
})();
