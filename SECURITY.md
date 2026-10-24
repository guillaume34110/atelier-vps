# Security

This service executes code supplied by its trusted operators. It is not a
multi-tenant sandbox for strangers. Jobs share UID 10001, a Linux container and
download caches; resource reservations do not isolate hostile code or secrets.

The client uses an existing restricted SSH key. Keep its private half local,
mode 0600, outside the repository. Check host fingerprints out of band and keep
strict host-key verification enabled. The forced gateway permits only status,
run, fetch and release; it does not grant a host shell or the Docker socket.

The MCP bridge listens on local stdio only. Do not expose it over HTTP without
authentication, authorization and an explicit review. Installing this repository
does not grant any third party access to the maintainer's VPS.

Report vulnerabilities through GitHub's private vulnerability reporting when
available. Do not publish keys, tokens, private payloads or reproduction data in
an issue. Until a fix is deployed, revoke the affected dedicated SSH key and
stop admitting new jobs if necessary; preserve existing useful results.
