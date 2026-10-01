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
})();
