#!/bin/bash
# Install the Atlas daemon and its deliberately thin Moonraker component.

set -euo pipefail

SRCDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ATLAS_USER="${SUDO_USER:-${USER}}"
ATLAS_HOME=""
ATLAS_REPO="${SRCDIR}"
ATLAS_DATA=""
MOONRAKER_DIR=""
PYTHON="$(command -v python3)"
SYSTEMD_DIR="/etc/systemd/system"
UDEV_DIR="/etc/udev/rules.d"
DESTDIR="${DESTDIR:-}"
NO_START=0

usage() {
    cat <<EOF
Usage: $0 [options]
  --user USER             service account (default: invoking user)
  --home PATH             account home (default: passwd database)
  --repo PATH             installed HELIX/Atlas checkout
  --data-dir PATH         printer data directory
  --moonraker-dir PATH    Moonraker checkout
  --python PATH           Python 3 executable
  --systemd-dir PATH      unit directory (default: /etc/systemd/system)
  --no-start              install only; do not enable/restart services

Set DESTDIR to stage an installation for packaging/tests. Staged installs never
run systemctl or change file ownership.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --user) ATLAS_USER="$2"; shift 2 ;;
        --home) ATLAS_HOME="$2"; shift 2 ;;
        --repo) ATLAS_REPO="$2"; shift 2 ;;
        --data-dir) ATLAS_DATA="$2"; shift 2 ;;
        --moonraker-dir) MOONRAKER_DIR="$2"; shift 2 ;;
        --python) PYTHON="$2"; shift 2 ;;
        --systemd-dir) SYSTEMD_DIR="$2"; shift 2 ;;
        --no-start) NO_START=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ -z "${ATLAS_HOME}" ]; then
    ATLAS_HOME="$(getent passwd "${ATLAS_USER}" | cut -d: -f6)"
fi
if [ -z "${ATLAS_HOME}" ]; then
    echo "Unable to determine home for ${ATLAS_USER}; pass --home" >&2
    exit 2
fi
ATLAS_DATA="${ATLAS_DATA:-${ATLAS_HOME}/printer_data}"
MOONRAKER_DIR="${MOONRAKER_DIR:-${ATLAS_HOME}/moonraker}"

for value in "${ATLAS_USER}" "${ATLAS_HOME}" "${ATLAS_REPO}" \
             "${ATLAS_DATA}" "${MOONRAKER_DIR}" "${PYTHON}"; do
    if [[ "${value}" =~ [[:space:]\&\|\$\`] ]]; then
        echo "Unsupported whitespace or metacharacter in path/value: ${value}" >&2
        exit 2
    fi
done

if [ -z "${DESTDIR}" ] && [ "${EUID}" -ne 0 ]; then
    echo "Run with sudo for a system install, or set DESTDIR for staging." >&2
    exit 1
fi
if [ ! -d "${MOONRAKER_DIR}/moonraker/components" ] && [ -z "${DESTDIR}" ]; then
    echo "Moonraker components directory not found: ${MOONRAKER_DIR}" >&2
    exit 1
fi

stage_path() {
    printf '%s%s' "${DESTDIR}" "$1"
}

STATE_DIR="${ATLAS_HOME}/.local/state/atlas"
STATE_FILE="${STATE_DIR}/status.json"
ENV_FILE="${ATLAS_DATA}/config/atlas.env"
MOONRAKER_CONF="${ATLAS_DATA}/config/moonraker.conf"
ASVC_FILE="${ATLAS_DATA}/moonraker.asvc"
COMPONENT_FILE="${MOONRAKER_DIR}/moonraker/components/atlas.py"
UNIT_FILE="${SYSTEMD_DIR}/atlas.service"
# Sort after generic Klipper tty rules so the Atlas account can issue the
# 1200-baud bootloader request as well as open the enumerated bootloader.
UDEV_FILE="${UDEV_DIR}/99-z-atlas-flash.rules"

install -d -m 0700 "$(stage_path "${STATE_DIR}")"
install -d -m 0755 "$(stage_path "${ATLAS_DATA}/config")"
install -d -m 0755 "$(stage_path "${MOONRAKER_DIR}/moonraker/components")"
install -d -m 0755 "$(stage_path "${SYSTEMD_DIR}")"
install -d -m 0755 "$(stage_path "${UDEV_DIR}")"

install -m 0644 "${SRCDIR}/moonraker_components/atlas.py" \
    "$(stage_path "${COMPONENT_FILE}")"

ENV_DEST="$(stage_path "${ENV_FILE}")"
if [ ! -f "${ENV_DEST}" ]; then
    install -m 0600 /dev/null "${ENV_DEST}"
    {
        printf 'ATLAS_LOG=%s\n' "${ATLAS_DATA}/logs/klippy.log"
        printf 'ATLAS_STATE=%s\n' "${STATE_FILE}"
        printf 'ATLAS_TELEMETRY=%s\n' "${ATLAS_DATA}/logs/atlas-telemetry.jsonl"
        printf 'ATLAS_CATALOG=%s\n' "${ATLAS_REPO}/atlas/diagnosis/patterns"
        printf 'ATLAS_INTERVAL=0.5\n'
        printf 'ATLAS_HEARTBEAT=5.0\n'
        printf 'ATLAS_MAX_EVENTS=2000\n'
        printf 'ATLAS_MODEL=\n'
        printf 'ATLAS_LLAMA_CLI=\n'
        printf 'ATLAS_ACCELERATOR=cpu\n'
        printf 'ATLAS_ASSISTANT_SOCKET=%s\n' "${STATE_DIR}/assistant.sock"
        printf 'ATLAS_MEMORY_FILE=%s\n' "${STATE_DIR}/memory.json"
        printf 'ATLAS_PRINTER_CONFIG=%s\n' \
            "${ATLAS_DATA}/config/printer.cfg"
    } > "${ENV_DEST}"
fi

UNIT_DEST="$(stage_path "${UNIT_FILE}")"
sed -e "s|@ATLAS_USER@|${ATLAS_USER}|g" \
    -e "s|@ATLAS_REPO@|${ATLAS_REPO}|g" \
    -e "s|@ATLAS_ENV@|${ENV_FILE}|g" \
    -e "s|@PYTHON@|${PYTHON}|g" \
    -e "s|@ATLAS_STATE_DIR@|${STATE_DIR}|g" \
    "${SRCDIR}/scripts/atlas.service.in" > "${UNIT_DEST}"
chmod 0644 "${UNIT_DEST}"

if [ -n "${DESTDIR}" ]; then
    ATLAS_FLASH_GROUP="${ATLAS_USER}"
else
    ATLAS_FLASH_GROUP="$(id -gn "${ATLAS_USER}")"
fi
UDEV_DEST="$(stage_path "${UDEV_FILE}")"
sed -e "s|@ATLAS_FLASH_GROUP@|${ATLAS_FLASH_GROUP}|g" \
    "${SRCDIR}/scripts/99-atlas-flash.rules.in" > "${UDEV_DEST}"
chmod 0644 "${UDEV_DEST}"

MOONRAKER_DEST="$(stage_path "${MOONRAKER_CONF}")"
touch "${MOONRAKER_DEST}"
if ! grep -q '^\[atlas\]$' "${MOONRAKER_DEST}"; then
    {
        printf '\n[atlas]\n'
        printf 'state_file: %s\n' "${STATE_FILE}"
        printf 'poll_interval: 0.5\n'
        printf 'stale_after: 15\n'
        printf 'assistant_socket: %s\n' "${STATE_DIR}/assistant.sock"
        printf 'assistant_timeout: 300\n'
    } >> "${MOONRAKER_DEST}"
fi

ASVC_DEST="$(stage_path "${ASVC_FILE}")"
touch "${ASVC_DEST}"
if ! grep -qx 'atlas' "${ASVC_DEST}"; then
    printf 'atlas\n' >> "${ASVC_DEST}"
fi

if [ -z "${DESTDIR}" ]; then
    chown -R "${ATLAS_USER}:${ATLAS_USER}" "${STATE_DIR}"
    chown "${ATLAS_USER}:${ATLAS_USER}" "${ENV_FILE}"
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=tty --action=change
    systemctl daemon-reload
    if [ "${NO_START}" -eq 0 ]; then
        systemctl enable --now atlas.service
        systemctl restart moonraker.service
    fi
fi

echo "Atlas installed. State: ${STATE_FILE}; Moonraker API: /server/atlas/status"
echo "Set ATLAS_MODEL and ATLAS_LLAMA_CLI in ${ENV_FILE} to enable the local assistant."
