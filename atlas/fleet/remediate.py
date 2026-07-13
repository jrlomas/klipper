# Fleet lockstep remediation reuses the signed provisioning executor.

from ..provision.execute import ProvisionBlocked


def remediate_board(report, executor, plan, signed_image,
                    confirmed=False, cancel=None):
    if report.in_lockstep:
        return []
    if report.action != "flash-board" or not report.requires_signed_flash:
        raise ProvisionBlocked(
            "coherence verdict does not authorize a signed board flash")
    return executor.execute(
        plan, signed_image, confirmed=confirmed, cancel=cancel)
