// Small enhancements. All pages also work without JavaScript / HTMX.
(function () {
  "use strict";
  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.content : "";
  }
  document.addEventListener("htmx:configRequest", function (e) {
    e.detail.headers["X-CSRF-Token"] = csrfToken();
  });
  // Show server-side error fragments (4xx/5xx) instead of silently ignoring them.
  document.addEventListener("htmx:beforeSwap", function (e) {
    var status = e.detail.xhr.status;
    if (status >= 400 && status < 600) {
      e.detail.shouldSwap = true;
      e.detail.isError = false;
    }
  });
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-toggle-nav]");
    if (t) {
      var nav = document.getElementById("sidebar");
      if (nav) nav.classList.toggle("open");
    }
  });

  // Post editor: live character counter (UTF-16 units, like Telegram), group filter and selected counter.
  // The server validates everything again; this is only a convenience.
  var form = document.querySelector("[data-post-form]");
  if (form) {
    var body = form.querySelector("[data-post-body]"), counter = form.querySelector("[data-post-counter]");
    var image = form.querySelector("[data-post-image]"), remove = form.querySelector("[data-remove-image]");
    var existing = !!form.querySelector(".thumb");
    var update = function () {
      var hasImage = (image && image.files && image.files.length > 0) || (existing && !(remove && remove.checked));
      var limit = parseInt(form.dataset[hasImage ? "captionLimit" : "textLimit"], 10);
      var used = body.value.replace(/\r\n/g, "\n").length + parseInt(form.dataset.footerUnits, 10);
      counter.textContent = used + " of " + limit + " characters (including the automatic footer)";
      counter.classList.toggle("counter-bad", used > limit);
    };
    [body, image, remove].forEach(function (el) { if (el) el.addEventListener("input", update); if (el) el.addEventListener("change", update); });
    update();
    var boxes = form.querySelectorAll("[data-group-box]"), count = form.querySelector("[data-selected-count]");
    var recount = function () { var n = 0; boxes.forEach(function (b) { if (b.checked) n++; }); if (count) count.textContent = n; };
    boxes.forEach(function (b) { b.addEventListener("change", recount); });
    recount();
    var filter = form.querySelector("[data-group-filter]");
    if (filter) filter.addEventListener("input", function () {
      var q = filter.value.toLowerCase();
      form.querySelectorAll("[data-group-row]").forEach(function (r) { r.style.display = r.dataset.title.indexOf(q) === -1 ? "none" : ""; });
    });
    var none = form.querySelector("[data-select-none]");
    if (none) none.addEventListener("click", function () { boxes.forEach(function (b) { b.checked = false; }); recount(); });
  }
  document.addEventListener("submit", function (e) {
    var msg = e.target.dataset ? e.target.dataset.confirm : "";
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
})();
