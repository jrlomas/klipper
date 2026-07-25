"""Differential check against the pinned, tested OpenAMS host reducer."""

import importlib.util
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "test"))

import openams_domain_test as corpus
from helix.openams import model as M


LEGACY_SOURCE = Path(
    os.environ.get(
        "OPENAMS_LEGACY_STATE",
        "/home/jrlomas/klipper_openams/src/oams_state.py",
    )
)


def _load_legacy():
    spec = importlib.util.spec_from_file_location(
        "openams_legacy_state", LEGACY_SOURCE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unit(unit):
    if unit is None:
        return None
    return (unit.node, unit.bay)


def _legacy_mode(legacy, mode):
    names = {
        M.LaneMode.UNLOADED: legacy.OP_UNLOADED,
        M.LaneMode.LOADING: legacy.OP_LOADING,
        M.LaneMode.LOADED: legacy.OP_LOADED,
        M.LaneMode.UNLOADING: legacy.OP_UNLOADING,
        M.LaneMode.RELOADING: legacy.OP_LOADED,
        M.LaneMode.CALIBRATING: legacy.OP_CALIBRATING,
    }
    return names.get(mode, mode.value.upper())


def _lane_projection_from_new(legacy, lane):
    runout = {
        M.RunoutPhase.IDLE: legacy.RUNOUT_IDLE,
        M.RunoutPhase.TAIL_BUDGET: legacy.RUNOUT_PAUSING,
        M.RunoutPhase.COASTING: legacy.RUNOUT_COASTING,
    }[lane.runout.phase]
    reload_target = None
    if lane.mode == M.LaneMode.RELOADING:
        runout = legacy.RUNOUT_LOADING
        reload_target = _unit(lane.active.target)
    pause_origin = (
        lane.runout.origin_mm
        if lane.runout.phase == M.RunoutPhase.TAIL_BUDGET
        else None
    )
    coast_origin = (
        lane.runout.origin_mm
        if lane.runout.phase == M.RunoutPhase.COASTING
        else None
    )
    prior_op = None
    if (
        lane.mode == M.LaneMode.CALIBRATING
        and lane.active.resume_mode is not None
    ):
        prior_op = _legacy_mode(legacy, lane.active.resume_mode)
    return {
        "op": _legacy_mode(legacy, lane.mode),
        "group": lane.group,
        "unit": _unit(lane.unit),
        "following": lane.follower.enabled,
        "direction": (
            int(lane.follower.direction)
            if lane.follower.enabled
            else None
        ),
        "runout": runout,
        "pause_origin": pause_origin,
        "coast_origin": coast_origin,
        "reload_target": reload_target,
        "op_deadline": (
            None if lane.active is None else lane.active.deadline_at
        ),
        "op_gen": lane.generation,
        "prior_op": prior_op,
        "since": lane.since,
        "message": lane.message,
    }


def _lane_projection_from_legacy(lane):
    return {
        "op": lane.op,
        "group": lane.group,
        "unit": lane.unit,
        "following": lane.following,
        "direction": lane.direction if lane.following else None,
        "runout": lane.runout,
        "pause_origin": (
            lane.pause_origin
            if lane.runout == "pausing"
            else None
        ),
        "coast_origin": (
            lane.coast_origin
            if lane.runout == "coasting"
            else None
        ),
        "reload_target": (
            lane.reload_target
            if lane.runout == "loading"
            else None
        ),
        "op_deadline": lane.op_deadline,
        "op_gen": lane.op_gen,
        "prior_op": lane.prior_op,
        "since": lane.since,
        "message": lane.message,
    }


def _legacy_system(legacy, system):
    lanes = {}
    for lane_id, lane in system.lanes.items():
        projection = _lane_projection_from_new(legacy, lane)
        direction = (
            int(lane.follower.direction)
            if not lane.follower.enabled
            else projection["direction"]
        )
        lanes[lane_id] = legacy.LaneState(
            op=projection["op"],
            group=projection["group"],
            unit=projection["unit"],
            following=projection["following"],
            direction=direction,
            runout=projection["runout"],
            pause_origin=projection["pause_origin"],
            coast_origin=projection["coast_origin"],
            reload_target=projection["reload_target"],
            op_deadline=projection["op_deadline"],
            op_gen=projection["op_gen"],
            prior_op=projection["prior_op"],
            since=projection["since"],
            message=projection["message"],
        )
    return legacy.SystemState(
        lanes=lanes,
        fw_owns_liveness=system.firmware_owns_liveness,
    )


def _legacy_observation(legacy, observation):
    if observation is None:
        return legacy.World()
    lanes = {}
    for lane_id, lane in observation.lanes.items():
        lanes[lane_id] = legacy.LaneWorld(
            extruder_pos=lane.extruder_position_mm,
            printing=lane.printing,
            loaded={_unit(key): value
                    for key, value in lane.loaded.items()},
            ready={_unit(key): value
                   for key, value in lane.ready.items()},
            group_bays={
                name: tuple(_unit(unit) for unit in units)
                for name, units in lane.groups.items()
            },
            path_len=dict(lane.path_length_mm),
            reload_before=lane.reload_before_mm,
        )
    return legacy.World(lanes=lanes)


def _legacy_event(legacy, event):
    if isinstance(event, M.Tick):
        return legacy.Tick()
    if isinstance(event, M.LoadGroup):
        return legacy.Load(event.lane, event.group)
    if isinstance(event, M.LoadUnit):
        return legacy.LoadBay(event.lane, _unit(event.unit))
    if isinstance(event, M.Unload):
        return legacy.Unload(event.lane)
    if isinstance(event, M.Cancel):
        return legacy.Cancel(event.lane)
    if isinstance(event, M.Calibrate):
        return legacy.Calibrate(
            event.lane,
            event.unit.node,
            event.unit.bay,
            event.kind.value,
        )
    if isinstance(event, M.OperationCompleted):
        return legacy.OpCompleted(
            event.lane,
            int(event.code),
            event.value,
            event.generation,
        )
    if isinstance(event, M.DeadlineExpired):
        return legacy.Timeout(event.lane)
    if isinstance(event, M.SetFollowerRequest):
        return legacy.Follow(
            event.lane, int(event.enabled), int(event.direction)
        )
    if isinstance(event, M.Resync):
        return legacy.ClearErrors()
    raise TypeError(type(event).__name__)


def _effect_projection(effect):
    name = type(effect).__name__
    if name == "StartOperation":
        kind = effect.operation.kind.value
        if kind == M.OperationKind.RELOAD.value:
            kind = M.OperationKind.LOAD.value
        return (
            "start",
            kind,
            _unit(effect.operation.target),
            effect.operation.generation,
            (
                None
                if effect.operation.calibration is None
                else effect.operation.calibration.value
            ),
        )
    if name in ("StartLoad", "StartUnload", "StartCalibrate"):
        kind = {
            "StartLoad": "load",
            "StartUnload": "unload",
            "StartCalibrate": "calibrate",
        }[name]
        return (
            "start",
            kind,
            effect.unit,
            effect.gen,
            getattr(effect, "kind", None),
        )
    if name in ("CancelOperation", "CancelLoad"):
        unit = _unit(effect.unit) if name == "CancelOperation" else effect.unit
        return ("cancel", unit)
    if name == "SetFollower":
        unit = (
            _unit(effect.unit)
            if isinstance(effect.unit, M.UnitRef)
            else effect.unit
        )
        return (
            "follower",
            unit,
            int(effect.enable if hasattr(effect, "enable") else effect.enabled),
            int(effect.direction),
        )
    if name == "Pause":
        return ("pause", effect.reason)
    if name == "ArmDeadline":
        duration = (
            effect.duration
            if hasattr(effect, "duration")
            else effect.seconds
        )
        return ("deadline.arm", duration)
    if name == "CancelDeadline":
        return ("deadline.cancel",)
    if name == "Settle":
        result = effect.result
        code = None if result.code is None else int(result.code)
        return (
            "settle",
            result.ok,
            code,
            result.message,
            result.value,
        )
    raise TypeError(name)


@unittest.skipUnless(
    LEGACY_SOURCE.exists(),
    "set OPENAMS_LEGACY_STATE to the pinned OpenAMS reducer",
)
class LegacyDifferentialTests(unittest.TestCase):
    def test_golden_corpus_matches_tested_legacy_reducer(self):
        legacy = _load_legacy()
        native_reduce = corpus.reduce
        comparisons = []

        def checked_reduce(system, event, observation=None):
            new_system, new_effects = native_reduce(
                system, event, observation
            )
            old_system, old_effects = legacy.reduce(
                _legacy_system(legacy, system),
                _legacy_event(legacy, event),
                _legacy_observation(legacy, observation),
                event.observed_at,
            )
            for lane_id, new_lane in new_system.lanes.items():
                comparisons.append(type(event).__name__)
                self.assertEqual(
                    _lane_projection_from_new(legacy, new_lane),
                    _lane_projection_from_legacy(
                        old_system.lanes[lane_id]
                    ),
                    "state diverged after %s" % type(event).__name__,
                )
            self.assertEqual(
                [_effect_projection(item) for item in new_effects],
                [_effect_projection(item) for item in old_effects],
                "effects diverged after %s" % type(event).__name__,
            )
            return new_system, new_effects

        corpus.reduce = checked_reduce
        try:
            corpus.build_traces()
        finally:
            corpus.reduce = native_reduce
        self.assertEqual(len(comparisons), 18)


if __name__ == "__main__":
    unittest.main()
