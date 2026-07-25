"""Qualified compiler target descriptions for the first ARM slice."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Target:
    name: str
    triple: str
    cpu: str
    llvm_arch: str
    float_abi: str = "soft"


TARGETS = {
    "stm32g0b1": Target(
        "stm32g0b1", "thumbv6m-none-eabi", "cortex-m0plus", "armv6-m"
    ),
    "rp2040": Target(
        "rp2040", "thumbv6m-none-eabi", "cortex-m0plus", "armv6-m"
    ),
    "stm32f767": Target(
        "stm32f767", "thumbv7em-none-eabi", "cortex-m7", "armv7e-m"
    ),
    "stm32h723": Target(
        "stm32h723", "thumbv7em-none-eabi", "cortex-m7", "armv7e-m"
    ),
}
