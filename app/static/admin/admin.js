/* Operator console behaviour. Vendored locally — no CDN, no inline scripts
   (the Content-Security-Policy requires a per-response nonce). */

(function () {
  "use strict";

  // Confirmation dialogs for destructive forms. Templates opt in with a
  // ``data-confirm`` attribute instead of an inline ``onsubmit`` handler,
  // which the strict CSP would block.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.tagName !== "FORM") {
      return;
    }
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  // Live prompt character counter. The textarea opts in with ``data-counter``
  // and its output element references the same id via ``data-counter-for``.
  document.querySelectorAll("[data-counter]").forEach(function (area) {
    var output = document.querySelector('[data-counter-for="' + area.id + '"]');
    if (!output) {
      return;
    }
    var max = area.getAttribute("maxlength") || "0";
    var update = function () {
      output.textContent = area.value.length + " / " + max;
    };
    area.addEventListener("input", update);
    update();
  });

  // Copy the live draft prompt into the sandbox payload just before submit so
  // "Testar" runs what the operator is currently editing, not the saved text.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.getAttribute("action") !== "/admin/agents/test") {
      return;
    }
    var source = document.getElementById("system_prompt");
    var target = form.querySelector("#draft_prompt");
    if (source && target) {
      target.value = source.value;
    }
  });
})();
