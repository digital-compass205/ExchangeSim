#!/bin/sh
# Install the exchange simulator on RHEL 8.
#
# Pure Python against the platform interpreter, so "installing" is copying the
# tree, creating a service account and dropping in the systemd unit. There is
# nothing to compile and nothing to download.
#
# Usage:  sudo ./deploy/install.sh [target-directory]

set -eu

TARGET="${1:-/opt/exchangesim}"
SERVICE_USER="exsim"
PYTHON="/usr/libexec/platform-python"
SOURCE="$(cd "$(dirname "$0")/.." && pwd)"

if [ "$(id -u)" -ne 0 ]; then
    echo "install.sh must run as root" >&2
    exit 1
fi

if [ ! -x "$PYTHON" ]; then
    echo "$PYTHON not found; this expects RHEL 8's platform-python" >&2
    exit 1
fi

VERSION="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$VERSION" in
    3.6|3.7|3.8|3.9|3.1[0-9]) ;;
    *) echo "unsupported Python $VERSION; 3.6 or newer is required" >&2; exit 1 ;;
esac

echo "installing from $SOURCE to $TARGET (python $VERSION)"

id -u "$SERVICE_USER" >/dev/null 2>&1 || \
    useradd --system --home-dir "$TARGET" --shell /sbin/nologin "$SERVICE_USER"

mkdir -p "$TARGET" "$TARGET/var" "$TARGET/var/log" "$TARGET/var/run" "$TARGET/config"
cp -r "$SOURCE/exchangesim" "$TARGET/"
cp -r "$SOURCE/bin" "$TARGET/"
cp -r "$SOURCE/scenarios" "$TARGET/" 2>/dev/null || true
cp -r "$SOURCE/tools" "$TARGET/" 2>/dev/null || true
cp "$SOURCE/README.md" "$SOURCE/DETAILED_DOC.md" "$TARGET/" 2>/dev/null || true

# Configs are deployment state: never overwrite one that is already in place.
for config in "$SOURCE"/config/*.json; do
    name="$(basename "$config")"
    if [ -f "$TARGET/config/$name" ]; then
        echo "  keeping existing config/$name"
    else
        cp "$config" "$TARGET/config/$name"
        echo "  installed config/$name"
    fi
done

chmod +x "$TARGET/bin/exchangesim"
chown -R "$SERVICE_USER:$SERVICE_USER" "$TARGET"
chmod 750 "$TARGET" "$TARGET/var"

# One command on the path, for the operator who is not going through systemd.
ln -sf "$TARGET/bin/exchangesim" /usr/local/bin/exchangesim

install -m 0644 "$SOURCE/deploy/exsimd@.service" \
        /etc/systemd/system/exsimd@.service
systemctl daemon-reload

cat <<EOF

installed to $TARGET

Run it, either way -- pick one, not both:

  As one command, logging to $TARGET/var/log. Run it as $SERVICE_USER,
  which is what owns that directory:
    sudo -u $SERVICE_USER exchangesim start
    exchangesim status
    exchangesim logs hkex -f
    exchangesim stop

  Or under systemd, one unit per venue, logging to the journal:
    systemctl enable --now exsimd@japannext
    systemctl status exsimd@japannext

Drive it:
    $PYTHON -m exchangesim.cli.exsim --port 9101 info

Run the scenario suite:
    $PYTHON -m exchangesim.scenario.runner "$TARGET/scenarios/*.json"

Each additional exchange is another config file in $TARGET/config plus an entry
in config/services.json (or another "systemctl enable --now exsimd@<name>");
give it its own FIX and control ports.
EOF
