# Notion Branded PDF Reports — smoke test

A static landing site to validate paid demand for **"Turn your Notion databases into client-ready PDF reports in minutes."** No product is built — no Notion API, no auth, no PDF generation, no real payments.

## Pages

| Page | Purpose |
|---|---|
| `index.html` | Landing: hero, before/after PDF mockup, benefits, use cases, how it works, pricing, FAQ |
| `pricing.html` | Full pricing (Starter $19/mo, Pro $49/mo) + FAQ |
| `checkout.html` | Fake checkout. After the "Start paid beta" click it shows: *"We're onboarding the first beta users. Leave your email for early access."* + email capture |

## Tracking

Every event is always logged to the browser console and to `localStorage` (`st_events`), and optionally forwarded to PostHog and/or a webhook.site beacon (see `assets/config.js`).

Attribution: `utm_source` / `utm_medium` / `utm_campaign` / `utm_content` / `utm_term` from the visitor's first landing URL (or the referrer, if no UTMs) are saved once per browser (`st_utm`) and attached to **every** event — so checkout attempts and signups can be segmented by traffic channel. Tag your ad/post links, e.g. `?utm_source=reddit&utm_campaign=notion-pdf-v1`.

| Event | Fires when |
|---|---|
| `page_view` | Any page loads |
| `pricing_view` | Pricing page loads, or the landing pricing section scrolls into view |
| `cta_click` | Any "Start paid beta" CTA (props: `location`) |
| `pricing_tier_click` | A plan button is clicked (props: `tier`, `location`) |
| `checkout_attempt` | The checkout button is clicked |
| `email_signup` | Email submitted (props: `tier`). Sent to Formspree if configured, else stored in `localStorage` (`st_emails`) |

Debug helpers in the browser console:

- `dumpEvents()` — raw event log for this browser
- `dumpEmails()` — locally captured emails
- `stats()` — funnel counts + conversion rates (visit → pricing → plan click → checkout attempt → email)

For real multi-visitor conversion rates, set `posthogKey` in `assets/config.js` and build the funnel in PostHog (free plan is enough).

## Deploy

Pure static site — deploy the repo root to Vercel (config included in `vercel.json`), Netlify or GitHub Pages. No build step.

## Success threshold

Validated if **≥ 3 unrelated visitors attempt checkout** (`checkout_attempt` from distinct `anonId`s / PostHog users) **or ≥ 10% of qualified traffic clicks a paid plan** (`pricing_tier_click` / `page_view` unique visitors).
