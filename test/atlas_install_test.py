#!/usr/bin/env python3
# Staged-root acceptance test for scripts/install-atlas.sh.

import os
import pathlib
import stat
import subprocess
import tempfile


ROOT = pathlib.Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "scripts" / "install-atlas.sh"


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_staged_install_is_complete_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        stage = pathlib.Path(tmp) / "root"
        env = dict(os.environ, DESTDIR=str(stage))
        command = [
            "bash", str(INSTALLER), "--user", "atlas-test",
            "--home", "/home/atlas-test", "--repo", "/opt/helix",
            "--data-dir", "/home/atlas-test/printer_data",
            "--moonraker-dir", "/home/atlas-test/moonraker",
            "--python", "/usr/bin/python3", "--no-start",
        ]
        subprocess.run(command, env=env, check=True, capture_output=True,
                       text=True)
        subprocess.run(command, env=env, check=True, capture_output=True,
                       text=True)

        home = stage / "home" / "atlas-test"
        env_file = home / "printer_data" / "config" / "atlas.env"
        state_dir = home / ".local" / "state" / "atlas"
        component = home / "moonraker" / "moonraker" / "components" / "atlas.py"
        unit = stage / "etc" / "systemd" / "system" / "atlas.service"
        moonraker = home / "printer_data" / "config" / "moonraker.conf"
        asvc = home / "printer_data" / "moonraker.asvc"
        udev = stage / "etc" / "udev" / "rules.d" / "99-z-atlas-flash.rules"

        assert _mode(env_file) == 0o600
        assert _mode(state_dir) == 0o700
        assert component.read_bytes() == (
            ROOT / "moonraker_components" / "atlas.py").read_bytes()
        unit_text = unit.read_text()
        assert "User=atlas-test" in unit_text
        assert "WorkingDirectory=/opt/helix" in unit_text
        assert "NoNewPrivileges=true" in unit_text
        assert "ProtectSystem=strict" in unit_text
        assert "ReadWritePaths=/home/atlas-test/.local/state/atlas" in unit_text
        assert moonraker.read_text().count("[atlas]") == 1
        assert asvc.read_text().splitlines().count("atlas") == 1
        assert "ATLAS_HEARTBEAT=5.0" in env_file.read_text()
        assert "ATLAS_TELEMETRY=" in env_file.read_text()
        assert "ATLAS_MODEL=" in env_file.read_text()
        assert "ATLAS_ASSISTANT_SOCKET=" in env_file.read_text()
        assert "ATLAS_PRINTER_CONFIG=" in env_file.read_text()
        udev_text = udev.read_text()
        assert udev_text.count('GROUP="atlas-test"') == 4
        assert 'SUBSYSTEM=="tty"' in udev_text
        assert 'ATTRS{idProduct}=="614e"' in udev_text
        assert 'ATTR{idVendor}=="0483"' in udev_text
        assert 'ATTR{idVendor}=="2e8a"' in udev_text
        assert 'ATTR{idVendor}=="1d50"' in udev_text
        print("PASS: staged install is complete, private, hardened, "
              "and idempotent")


def test_installer_rejects_unsafe_paths():
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, DESTDIR=tmp)
        result = subprocess.run(
            ["bash", str(INSTALLER), "--user", "atlas-test", "--home",
             "/home/bad path", "--no-start"], env=env,
            capture_output=True, text=True)
        assert result.returncode == 2
        assert "Unsupported whitespace" in result.stderr
        print("PASS: installer rejects values unsafe for env/systemd expansion")


def main():
    test_staged_install_is_complete_and_idempotent()
    test_installer_rejects_unsafe_paths()
    print("ALL PASS")


if __name__ == "__main__":
    main()
