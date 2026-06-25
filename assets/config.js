/*
 * Configuración central del smoke test.
 * Por defecto TODO está vacío => el sitio funciona sin ninguna cuenta:
 * los eventos se ven en la consola del navegador y en localStorage ("st_events").
 *
 * Para activar servicios reales, rellena las claves de abajo:
 *  - posthogKey / posthogHost : analytics y embudo (https://posthog.com, plan gratis)
 *  - formspreeId              : recibir emails en tu bandeja (https://formspree.io, plan gratis)
 *  - beaconUrl                : ver eventos en vivo sin cuenta (crea un bin en https://webhook.site
 *                               y pega aquí la URL "Your unique URL")
 */
window.SMOKE_CONFIG = {
  // Analytics (PostHog). Deja vacío para no usarlo.
  posthogKey: "",
  posthogHost: "https://us.i.posthog.com",

  // Captura de email (Formspree). Pega solo el ID del form, ej. "xmyzabcd".
  formspreeId: "xvzjkwvb",

  // Beacon opcional para ver eventos en vivo sin cuenta (webhook.site).
  beaconUrl: "",

  // Datos del producto (para mantener el copy en un solo lugar).
  product: {
    name: "ClickUp Client PDF Reports",
    tagline: "Turn ClickUp tasks into client-ready PDF reports in minutes.",
    plans: {
      starter: { label: "Starter", price: 19 },
      pro: { label: "Pro", price: 49 }
    }
  }
};
