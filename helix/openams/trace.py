"""Canonical OpenAMS semantic traces for differential verification."""

from . import model as M


def _unit(unit):
    if unit is None:
        return None
    return [unit.node, unit.bay]


def lane_snapshot(lane):
    phase = lane.runout.phase.value
    if lane.mode == M.LaneMode.RELOADING:
        phase = "reloading"
    operation = None
    if lane.active is not None:
        operation = {
            "kind": lane.active.kind.value,
            "generation": lane.active.generation,
            "target": _unit(lane.active.target),
            "deadline_at": lane.active.deadline_at,
            "calibration": (
                None
                if lane.active.calibration is None
                else lane.active.calibration.value
            ),
            "resume_mode": (
                None
                if lane.active.resume_mode is None
                else lane.active.resume_mode.value
            ),
        }
    return {
        "status": "%s/%s" % (lane.mode.value, phase),
        "group": lane.group,
        "unit": _unit(lane.unit),
        "follower": {
            "enabled": lane.follower.enabled,
            "direction": int(lane.follower.direction),
        },
        "runout_origin_mm": lane.runout.origin_mm,
        "operation": operation,
        "generation": lane.generation,
        "message": lane.message,
    }


def effect_snapshot(effect):
    if isinstance(effect, M.StartOperation):
        return {
            "effect": "operation.start",
            "lane": effect.lane,
            "kind": effect.operation.kind.value,
            "generation": effect.operation.generation,
            "target": _unit(effect.operation.target),
            "calibration": (
                None
                if effect.operation.calibration is None
                else effect.operation.calibration.value
            ),
        }
    if isinstance(effect, M.CancelOperation):
        return {
            "effect": "operation.cancel",
            "lane": effect.lane,
            "generation": effect.generation,
            "target": _unit(effect.unit),
        }
    if isinstance(effect, M.SetFollower):
        return {
            "effect": "follower.set",
            "lane": effect.lane,
            "target": _unit(effect.unit),
            "enabled": effect.enabled,
            "direction": int(effect.direction),
        }
    if isinstance(effect, M.Pause):
        return {
            "effect": "print.pause",
            "lane": effect.lane,
            "reason": effect.reason,
        }
    if isinstance(effect, M.ArmDeadline):
        return {
            "effect": "deadline.arm",
            "lane": effect.lane,
            "generation": effect.generation,
            "duration": effect.duration,
        }
    if isinstance(effect, M.CancelDeadline):
        return {
            "effect": "deadline.cancel",
            "lane": effect.lane,
            "generation": effect.generation,
        }
    if isinstance(effect, M.Settle):
        return {
            "effect": "request.settle",
            "lane": effect.lane,
            "ok": effect.result.ok,
            "code": (
                None
                if effect.result.code is None
                else int(effect.result.code)
            ),
            "message": effect.result.message,
            "value": effect.result.value,
        }
    raise TypeError("unknown OpenAMS effect %s" % type(effect).__name__)


def transition_snapshot(system, effects, lane_id):
    return {
        "lane": lane_snapshot(system.lanes[lane_id]),
        "effects": [effect_snapshot(effect) for effect in effects],
    }


__all__ = [
    "effect_snapshot",
    "lane_snapshot",
    "transition_snapshot",
]
