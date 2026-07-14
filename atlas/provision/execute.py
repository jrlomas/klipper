# Non-shell, auditable provisioning execution jobs.

import filecmp
import json
import os
import subprocess
import tempfile
import time


class ProvisionBlocked(RuntimeError):
    pass


def verify_detached(image, public_key, signature=None):
    """Verify image.sig with an Ed25519 public key; unavailable backends fail closed."""
    signature = signature or image + ".sig"
    try:
        pub = bytes.fromhex(open(public_key).read().split()[0])
        sig = open(signature, "rb").read()
        message = open(image, "rb").read()
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import (
                Ed25519PublicKey)
            Ed25519PublicKey.from_public_bytes(pub).verify(sig, message)
        except ImportError:
            from nacl.signing import VerifyKey
            VerifyKey(pub).verify(message, sig)
    except Exception:
        return False
    return True


class ProvisionExecutor:
    def __init__(self, audit_path, runner=None, verifier=None,
                 public_key=None, wall_clock=time.time):
        self.audit_path = os.path.abspath(os.path.expanduser(audit_path))
        self.runner = runner or self._run
        self.verifier = verifier or (
            (lambda image: verify_detached(image, public_key))
            if public_key else (lambda image: False))
        self.clock = wall_clock

    @staticmethod
    def _run(argv, cwd):
        return subprocess.run(argv, cwd=cwd, check=True, capture_output=True,
                              text=True)

    def execute(self, plan, signed_image, confirmed=False, cancel=None):
        if plan.blockers:
            raise ProvisionBlocked("; ".join(plan.blockers))
        if plan.needs_confirmation and not confirmed:
            raise ProvisionBlocked("explicit flash confirmation required")
        image = os.path.abspath(os.path.expanduser(signed_image))
        if not os.path.isfile(image):
            raise ProvisionBlocked("signed image does not exist")
        if not self.verifier(image):
            raise ProvisionBlocked("signed image verification required")
        cwd = os.path.abspath(os.path.expanduser(plan.klipper_dir))
        os.makedirs(cwd, exist_ok=True)
        config_path = os.path.join(cwd, plan.config_out)
        build_root = os.path.join(cwd, "out")
        try:
            if os.path.commonpath([image, build_root]) == build_root:
                raise ProvisionBlocked(
                    "signed image must be outside the build output directory")
        except ValueError:
            pass
        with open(config_path, "w") as handle:
            for key, value in plan.kconfig.items():
                if value in ("n", False, None):
                    handle.write("# %s is not set\n" % key)
                else:
                    handle.write("%s=%s\n" % (
                        key, "y" if value in ("y", True) else value))
        kconfig_arg = "KCONFIG_CONFIG=%s" % config_path
        commands = [["make", kconfig_arg, "clean"],
                    ["make", kconfig_arg, "olddefconfig"],
                    ["make", kconfig_arg]]
        completed = []
        started = self.clock()
        try:
            for command in commands:
                if cancel is not None and cancel():
                    raise ProvisionBlocked("job cancelled before %s" % command[0])
                self.runner(command, cwd)
                completed.append(command)
            built = self._built_artifact(plan, cwd)
            if not os.path.isfile(built):
                raise ProvisionBlocked("build did not produce %s" % built)
            # Verify again after the build. This closes the case where the
            # supplied image aliases the build output and was overwritten
            # after the preflight verification.
            if not self.verifier(image):
                raise ProvisionBlocked("signed image verification required")
            if not filecmp.cmp(built, image, shallow=False):
                raise ProvisionBlocked(
                    "built artifact does not match the verified signed image")
            flash_command = self._flash_command(plan, image)
            if cancel is not None and cancel():
                raise ProvisionBlocked("job cancelled before %s"
                                       % flash_command[0])
            self.runner(flash_command, cwd)
            completed.append(flash_command)
        except Exception as exc:
            self._audit(plan, image, started, "failed", completed, str(exc))
            raise
        self._audit(plan, image, started, "complete", completed, "")
        return completed

    @staticmethod
    def _built_artifact(plan, cwd):
        filename = ("klipper.uf2" if plan.method == "rp2040-usb"
                    else "klipper.bin")
        return os.path.join(cwd, "out", filename)

    @staticmethod
    def _flash_command(plan, image):
        ident = plan.target_identifier
        if plan.method == "dfu":
            address = "0x08000000"
            for key in plan.kconfig:
                prefix = "CONFIG_STM32_FLASH_START_"
                if key.startswith(prefix):
                    address = "0x0800%s" % key[len(prefix):].zfill(4)
            return ["dfu-util", "-d", ident or "0483:df11", "-a", "0",
                    "-s", address + ":leave", "-D", image]
        if plan.method == "katapult-can":
            return ["python3", "lib/katapult/scripts/flash_can.py", "-i",
                    "can0", "-u", ident, "-f", image]
        if plan.method == "rp2040-usb":
            return ["python3", "scripts/flash_usb.py", "-t", plan.mcu,
                    "-d", ident, image]
        if plan.method == "katapult-usb":
            address = 0x08000000
            for key in plan.kconfig:
                prefix = "CONFIG_STM32_FLASH_START_"
                if key.startswith(prefix):
                    address = int("0800%s" % key[len(prefix):].zfill(4), 16)
            return ["python3", "scripts/flash_usb.py", "-t", plan.mcu,
                    "-d", ident, "-s", str(address), image]
        if plan.method == "serial":
            raise ProvisionBlocked(
                "serial flashing cannot guarantee the verified image path")
        raise ProvisionBlocked("flash method %s requires manual action"
                               % plan.method)

    def _audit(self, plan, image, started, status, commands, error):
        directory = os.path.dirname(self.audit_path)
        os.makedirs(directory, exist_ok=True)
        records = []
        try:
            with open(self.audit_path) as handle:
                records = json.load(handle)
        except FileNotFoundError:
            pass
        records.append({
            "board_id": plan.board_id, "method": plan.method,
            "target": plan.target_identifier, "image": os.path.basename(image),
            "started_at": started, "finished_at": self.clock(),
            "status": status, "commands": commands, "error": error,
        })
        fd, tmp = tempfile.mkstemp(prefix=".atlas-provision-", dir=directory)
        with os.fdopen(fd, "w") as handle:
            json.dump(records, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.audit_path)
        os.chmod(self.audit_path, 0o600)
