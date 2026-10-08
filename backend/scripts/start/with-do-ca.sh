#!/bin/bash
# MyBya patch — installs DigitalOcean's managed-database CA, then execs the
# real start script.
#
# WHY: DO managed databases present a certificate signed by a self-signed,
# per-project private CA that is in no public trust store. Open Wearables'
# redis client runs with ssl_cert_reqs=required (REDIS_SSL=true), so without
# this CA every Valkey connection dies with CERTIFICATE_VERIFY_FAILED.
#
# WHY A SCRIPT AND NOT AN INLINE run_command: DigitalOcean App Platform does
# NOT evaluate run_command through a shell — it splits it into argv. A
# multi-line command with a redirect was passed to printf as literal
# arguments, which printed them and exited 0 ("DeployContainerExitZero").
# Invoke this with no shell metacharacters:
#     run_command: scripts/start/with-do-ca.sh scripts/start/app.sh
#
# update-ca-certificates APPENDS to the trust store; it does not replace it,
# so outbound HTTPS to Garmin/Oura keeps working. Do not use SSL_CERT_FILE,
# which DOES replace the bundle and would break every provider call.
set -euo pipefail

if [ -n "${DO_DB_CA_PEM:-}" ]; then
  printf '%s\n' "$DO_DB_CA_PEM" > /usr/local/share/ca-certificates/do-managed-db.crt
  update-ca-certificates >/dev/null 2>&1
  echo "with-do-ca: installed DO managed-database CA"
else
  echo "with-do-ca: DO_DB_CA_PEM unset — skipping CA install" >&2
fi

exec "$@"
