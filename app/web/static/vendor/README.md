# Vendored front-end libraries

`htmx.min.js` (HTMX 2.x) is served from this folder so the running application never loads
scripts from a CDN. It is downloaded and checksum-verified by `python scripts/vendor_htmx.py`
(the Docker build runs it automatically if the file is missing).
The application still works without it: every page uses normal forms, HTMX only enhances them.
