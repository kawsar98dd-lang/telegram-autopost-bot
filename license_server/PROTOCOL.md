# License protocol v1 (seller reference)

Client (customer installation) → `POST {LICENSE_SERVER_URL}/v1/activate` and `/v1/verify`
JSON body: `license_key, product, installation_id, host, app_version, nonce, protocol`.

## Success: HTTP 200
`{"payload": {...}, "signature": "<base64url Ed25519 over canonical JSON of payload>"}`

Payload fields: `v, license_id, product, installation_id, host, status, nonce, issued_at,
expires_at (unix or null), verify_interval_seconds, offline_grace_seconds`.
`status` is one of `active, revoked, disabled, expired, deactivated` and is **signed**.
`nonce` must echo the request nonce (replay protection). Canonical JSON =
`json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`.

## Refusals (activation time only): HTTP 4xx, unsigned
`{"error": "invalid_key" | "product_mismatch" | "activation_limit" | ..., "message": "..."}`
The client never deactivates an existing installation because of an unsigned refusal;
revocation is delivered as a signed `status`.

## Seller operations
* revoke / disable / reactivate: change `licenses.status`; next `/v1/verify` returns the signed status.
* reset / transfer: set `activations.released_at`; the old installation gets `deactivated`.
* activation limit: refuse `/v1/activate` when active activations >= `max_activations`
  (an already-registered `installation_id` may always re-activate).
