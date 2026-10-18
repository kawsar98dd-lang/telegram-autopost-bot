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
      if (nav) {
        var isOpen = nav.classList.toggle("open");
        t.setAttribute("aria-expanded", isOpen ? "true" : "false");
      }
    }
  });

  // Keep the mobile navigation accessible and easy to dismiss.
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      var nav = document.getElementById("sidebar");
      var toggle = document.querySelector("[data-toggle-nav]");
      if (nav && nav.classList.contains("open")) {
        nav.classList.remove("open");
        if (toggle) { toggle.setAttribute("aria-expanded", "false"); toggle.focus(); }
      }
    }
  });
  document.addEventListener("click", function (e) {
    var nav = document.getElementById("sidebar");
    var toggle = document.querySelector("[data-toggle-nav]");
    if (!nav || !toggle) return;
    if (e.target.closest("#sidebar a")) {
      if (window.matchMedia("(max-width: 760px)").matches) nav.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
      return;
    }
    // Dismiss the mobile menu when the user taps outside it.
    if (window.matchMedia("(max-width: 760px)").matches && nav.classList.contains("open") &&
        !e.target.closest("#sidebar") && !e.target.closest("[data-toggle-nav]")) {
      nav.classList.remove("open");
      toggle.setAttribute("aria-expanded", "false");
    }
  });

  // Give headed data tables useful labels when they switch to a mobile record layout.
  // The table remains a normal semantic table on larger screens and keeps all cells/actions.
  function prepareResponsiveTables(root) {
    (root || document).querySelectorAll("table").forEach(function (table) {
      var headers = table.querySelectorAll("thead th");
      if (!headers.length) return;
      var labels = Array.prototype.map.call(headers, function (th) {
        return (th.textContent || "").replace(/\s+/g, " ").trim();
      });
      table.setAttribute("data-responsive-cards", "");
      table.querySelectorAll("tbody tr").forEach(function (row) {
        Array.prototype.forEach.call(row.cells, function (cell, index) {
          if (!cell.hasAttribute("data-label")) cell.setAttribute("data-label", labels[index] || "");
        });
      });
    });
  }
  prepareResponsiveTables();
  document.addEventListener("htmx:afterSwap", function (e) { prepareResponsiveTables(e.target); });

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
