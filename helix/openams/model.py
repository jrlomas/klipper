"""Typed OpenAMS state, events, observations, and semantic effects."""

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Mapping, Optional, Tuple


class LaneMode(str, Enum):
    UNKNOWN = "unknown"
    UNLOADED = "unloaded"
    LOADING = "loading"
    LOADED = "loaded"
    UNLOADING = "unloading"
    RELOADING = "reloading"
    CALIBRATING = "calibrating"
    HELD = "held"
    FAULT = "fault"


class RunoutPhase(str, Enum):
    IDLE = "idle"
    TAIL_BUDGET = "tail_budget"
    COASTING = "coasting"


class OperationKind(str, Enum):
    LOAD = "load"
    UNLOAD = "unload"
    CALIBRATE = "calibrate"
    RELOAD = "reload"


class CalibrationKind(str, Enum):
    PTFE = "ptfe"
    HUB = "hub_hes"


class Direction(IntEnum):
    REVERSE = 0
    FORWARD = 1


class ResultCode(IntEnum):
    SUCCESS = 0
    ERROR_UNSPECIFIED = 1
    ERROR_BUSY = 2
    SPOOL_ALREADY_IN_BAY = 3
    NO_SPOOL_IN_BAY = 4
    ERROR_KLIPPER_CALL = 5
    CANCEL = 6
    TIMEOUT = 7


@dataclass(frozen=True, order=True)
class UnitRef:
    node: int
    bay: int


@dataclass(frozen=True)
class FollowerState:
    enabled: bool = False
    direction: Direction = Direction.FORWARD


@dataclass(frozen=True)
class RunoutState:
    phase: RunoutPhase = RunoutPhase.IDLE
    origin_mm: Optional[float] = None


@dataclass(frozen=True)
class ActiveOperation:
    kind: OperationKind
    generation: int
    target: UnitRef
    deadline_at: float
    calibration: Optional[CalibrationKind] = None
    resume_mode: Optional[LaneMode] = None


@dataclass(frozen=True)
class LaneState:
    mode: LaneMode = LaneMode.UNLOADED
    group: Optional[str] = None
    unit: Optional[UnitRef] = None
    follower: FollowerState = field(default_factory=FollowerState)
    runout: RunoutState = field(default_factory=RunoutState)
    active: Optional[ActiveOperation] = None
    generation: int = 0
    since: float = 0.0
    message: Optional[str] = None


@dataclass(frozen=True)
class SystemState:
    lanes: Mapping[str, LaneState] = field(default_factory=dict)
    firmware_owns_liveness: bool = False


@dataclass(frozen=True)
class LaneObservation:
    observed_at: float = 0.0
    extruder_position_mm: float = 0.0
    printing: bool = False
    loaded: Mapping[UnitRef, bool] = field(default_factory=dict)
    ready: Mapping[UnitRef, bool] = field(default_factory=dict)
    groups: Mapping[str, Tuple[UnitRef, ...]] = field(default_factory=dict)
    path_length_mm: Mapping[int, float] = field(default_factory=dict)
    reload_before_mm: float = 0.0


@dataclass(frozen=True)
class Observation:
    lanes: Mapping[str, LaneObservation] = field(default_factory=dict)

    def lane(self, lane_id):
        return self.lanes.get(lane_id, LaneObservation())


@dataclass(frozen=True)
class Tick:
    observed_at: float


@dataclass(frozen=True)
class LoadGroup:
    lane: str
    group: str
    observed_at: float


@dataclass(frozen=True)
class LoadUnit:
    lane: str
    unit: UnitRef
    observed_at: float


@dataclass(frozen=True)
class Unload:
    lane: str
    observed_at: float


@dataclass(frozen=True)
class Cancel:
    lane: str
    observed_at: float


@dataclass(frozen=True)
class Calibrate:
    lane: str
    unit: UnitRef
    kind: CalibrationKind
    observed_at: float


@dataclass(frozen=True)
class OperationCompleted:
    lane: str
    generation: int
    code: ResultCode
    observed_at: float
    value: Optional[int] = None


@dataclass(frozen=True)
class DeadlineExpired:
    lane: str
    generation: int
    observed_at: float


@dataclass(frozen=True)
class SetFollowerRequest:
    lane: str
    enabled: bool
    direction: Direction
    observed_at: float


@dataclass(frozen=True)
class Resync:
    observed_at: float


@dataclass(frozen=True)
class OperationResult:
    ok: bool
    code: Optional[ResultCode]
    message: str
    value: Optional[int] = None


@dataclass(frozen=True)
class StartOperation:
    lane: str
    operation: ActiveOperation


@dataclass(frozen=True)
class CancelOperation:
    lane: str
    unit: UnitRef
    generation: int


@dataclass(frozen=True)
class SetFollower:
    lane: str
    unit: UnitRef
    enabled: bool
    direction: Direction


@dataclass(frozen=True)
class Pause:
    lane: str
    reason: str


@dataclass(frozen=True)
class ArmDeadline:
    lane: str
    generation: int
    duration: float


@dataclass(frozen=True)
class CancelDeadline:
    lane: str
    generation: int


@dataclass(frozen=True)
class Settle:
    lane: str
    result: OperationResult


WORLD_EVENTS = (Tick, LoadGroup, LoadUnit, Resync)


def validate_lane(lane):
    """Return invariant errors for one structured lane state."""
    errors = []
    operation_modes = {
        LaneMode.LOADING: OperationKind.LOAD,
        LaneMode.UNLOADING: OperationKind.UNLOAD,
        LaneMode.RELOADING: OperationKind.RELOAD,
        LaneMode.CALIBRATING: OperationKind.CALIBRATE,
    }
    required = operation_modes.get(lane.mode)
    if required is None and lane.active is not None:
        errors.append("inactive lane carries an active operation")
    if required is not None:
        if lane.active is None:
            errors.append("%s lane lacks an active operation" % lane.mode.value)
        elif lane.active.kind != required:
            errors.append(
                "%s lane carries %s operation"
                % (lane.mode.value, lane.active.kind.value)
            )
        elif lane.active.generation != lane.generation:
            errors.append("active operation generation is not current")
    if lane.mode == LaneMode.UNLOADED:
        if lane.unit is not None or lane.group is not None:
            errors.append("unloaded lane retains material identity")
        if lane.follower.enabled:
            errors.append("unloaded lane has follower enabled")
    if lane.mode in (
        LaneMode.LOADING,
        LaneMode.LOADED,
        LaneMode.UNLOADING,
        LaneMode.RELOADING,
    ) and lane.unit is None:
        errors.append("%s lane has no loaded unit" % lane.mode.value)
    if lane.mode != LaneMode.LOADED and (
        lane.runout.phase != RunoutPhase.IDLE
        or lane.runout.origin_mm is not None
    ):
        errors.append("runout substate exists outside loaded mode")
    if (
        lane.runout.phase == RunoutPhase.IDLE
        and lane.runout.origin_mm is not None
    ):
        errors.append("idle runout state retains an origin")
    if (
        lane.runout.phase != RunoutPhase.IDLE
        and lane.runout.origin_mm is None
    ):
        errors.append("active runout state lacks an origin")
    if not 0 <= lane.generation <= 255:
        errors.append("operation generation is outside one byte")
    return tuple(errors)


def describe_code(code):
    descriptions = {
        ResultCode.ERROR_BUSY: "OAMS is busy with another operation",
        ResultCode.SPOOL_ALREADY_IN_BAY:
            "filament already detected in the hub",
        ResultCode.NO_SPOOL_IN_BAY: "no spool present in the bay",
        ResultCode.ERROR_KLIPPER_CALL: "stopped by klipper monitor",
        ResultCode.TIMEOUT:
            "no filament progress (jam, dead motor, or missing sensor)",
        ResultCode.CANCEL: "cancelled",
    }
    return descriptions.get(code, "code %s" % int(code))


__all__ = [
    name for name in globals()
    if not name.startswith("_")
    and name not in (
        "Enum",
        "IntEnum",
        "Mapping",
        "Optional",
        "Tuple",
        "dataclass",
        "field",
    )
]
