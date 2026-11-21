#!/usr/bin/env bash
# Disposable Ubuntu Actions runner only. Requires the two CI PostgreSQL clusters.
set -euo pipefail
test "${GITHUB_ACTIONS:-}" = true
test -d /run/systemd/system
sudo apt-get install -y python3-venv
sudo groupadd --system clashbolt-worker
sudo install -d -m 755 /var/lib/clashbolt-target-root /opt/clashbolt-fixture
sudo /usr/bin/python3 -m venv /opt/clashbolt-fixture
# Install a wheel, so the target namespace never needs the checkout.
python -m pip wheel --no-deps --wheel-dir "$RUNNER_TEMP/fixture-wheels" .
sudo /opt/clashbolt-fixture/bin/pip install "$RUNNER_TEMP"/fixture-wheels/clashbolt-*.whl
version=$(ls /usr/lib/postgresql | sort -V | tail -1)
hba="/etc/postgresql/$version/clashbolt_target/pg_hba.conf"
sudo sed -i '1i local all /^cb_runner_ scram-sha-256' "$hba"
sudo pg_ctlcluster "$version" clashbolt_target reload
sudo install -m 644 deploy/systemd/clashbolt-target{.socket,@.service} /etc/systemd/system/
sudo touch /etc/clashbolt-host-canary
sudo install -m 644 ci/fixture_preflight.py /opt/clashbolt-fixture/preflight.py
sudo install -d /run/systemd/system/clashbolt-target@.service.d
printf '%s\n' '[Service]' 'ExecStartPre=/opt/clashbolt-fixture/bin/python -I /opt/clashbolt-fixture/preflight.py' |
    sudo tee /run/systemd/system/clashbolt-target@.service.d/ci.conf >/dev/null
sudo systemd-analyze verify clashbolt-target.socket clashbolt-target@ci.service
sudo systemctl daemon-reload
sudo systemctl start clashbolt-target.socket
# GitHub's runner sudo policy rejects a direct -g group switch. Enter the
# socket group via a root-launched setpriv, then drop back to the runner UID.
# Preserve the two test DSNs as environment, not command-line arguments.
sudo --preserve-env=CLASHBOLT_TEST_TARGET_ADMIN_URL,CLASHBOLT_TEST_DATABASE_URL \
    setpriv --reuid="$(id -u)" \
    --regid="$(getent group clashbolt-worker | cut -d: -f3)" --clear-groups \
    env CLASHBOLT_TARGET_TRANSPORT=systemd "$(command -v python)" \
    -m pytest -q backend/tests/test_target_host.py
