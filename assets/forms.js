/*
 * Captura de email del smoke test.
 * Busca un <form data-email-form> y al enviarlo:
 *   - Si hay formspreeId en config.js => envía el email a Formspree (AJAX, sin recargar).
 *   - Si no => "captura local": guarda el email en localStorage ("st_emails").
 * En ambos casos dispara el evento email_signup (con el tier) y muestra confirmación.
 */
(function () {
  var CFG = (window.SMOKE_CONFIG || {});

  function saveEmailLocal(payload) {
    try {
      var arr = JSON.parse(localStorage.getItem("st_emails") || "[]");
      arr.push(payload);
      localStorage.setItem("st_emails", JSON.stringify(arr));
    } catch (e) { /* ignorar */ }
  }

  function showSuccess(form) {
    var success = document.querySelector("[data-email-success]");
    if (success) {
      form.style.display = "none";
      success.hidden = false;
      success.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }

  function getTier() {
    var params = new URLSearchParams(location.search);
    var tier = (params.get("tier") || "").toLowerCase();
    return (tier === "starter" || tier === "pro") ? tier : "unspecified";
  }

  function handleSubmit(form, e) {
    e.preventDefault();
    var emailInput = form.querySelector('input[type="email"]');
    var email = (emailInput && emailInput.value || "").trim();
    if (!email) return;

    var tier = getTier();
    var payload = { email: email, tier: tier, ts: new Date().toISOString() };

    if (window.track) window.track("email_signup", { tier: tier });

    if (CFG.formspreeId) {
      // Envío real a Formspree.
      fetch("https://formspree.io/f/" + CFG.formspreeId, {
        method: "POST",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      })
        .then(function () { showSuccess(form); })
        .catch(function () { saveEmailLocal(payload); showSuccess(form); });
    } else {
      // Captura local (sin cuenta). Visible en localStorage "st_emails".
      saveEmailLocal(payload);
      console.log("[email captured locally]", payload);
      showSuccess(form);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var form = document.querySelector("[data-email-form]");
    if (form) {
      form.addEventListener("submit", function (e) { handleSubmit(form, e); });
    }
  });

  // Util de depuración.
  window.dumpEmails = function () {
    try { return JSON.parse(localStorage.getItem("st_emails") || "[]"); }
    catch (e) { return []; }
  };
})();
