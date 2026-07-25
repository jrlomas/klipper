"""Pure hierarchical OpenAMS material-lane and runout reducer."""

from dataclasses import replace

from .model import (
    ActiveOperation,
    ArmDeadline,
    CalibrationKind,
    Calibrate,
    Cancel,
    CancelDeadline,
    CancelOperation,
    DeadlineExpired,
    Direction,
    FollowerState,
    LaneMode,
    LaneState,
    LoadGroup,
    LoadUnit,
    Observation,
    OperationCompleted,
    OperationKind,
    OperationResult,
    Pause,
    Resync,
    ResultCode,
    RunoutPhase,
    RunoutState,
    SetFollower,
    SetFollowerRequest,
    Settle,
    StartOperation,
    SystemState,
    Tick,
    Unload,
    UnitRef,
    describe_code,
    validate_lane,
)


PAUSE_DISTANCE_MM = 60.0
ACTION_TIMEOUT = 120.0
DISCONNECT_BACKSTOP = 300.0


def initial_system(lane_ids):
    return SystemState(lanes={name: LaneState() for name in lane_ids})


def reduce(system, event, observation=None):
    """Reduce one event into immutable state and semantic effects."""
    observation = observation or Observation()
    if isinstance(event, Resync):
        lanes = {
            lane_id: _resync_lane(
                observation.lane(lane_id), event.observed_at
            )
            for lane_id in system.lanes
        }
        return replace(system, lanes=lanes), ()

    if isinstance(event, Tick):
        lanes = dict(system.lanes)
        effects = []
        for lane_id, lane in system.lanes.items():
            changed, emitted = _reduce_lane(
                lane,
                event,
                observation.lane(lane_id),
                lane_id,
                system.firmware_owns_liveness,
            )
            lanes[lane_id] = changed
            effects.extend(emitted)
        result = replace(system, lanes=lanes)
        _validate_system(result)
        return result, tuple(effects)

    lane_id = getattr(event, "lane", None)
    if lane_id is None or lane_id not in system.lanes:
        return system, ()
    changed, effects = _reduce_lane(
        system.lanes[lane_id],
        event,
        observation.lane(lane_id),
        lane_id,
        system.firmware_owns_liveness,
    )
    lanes = dict(system.lanes)
    lanes[lane_id] = changed
    result = replace(system, lanes=lanes)
    _validate_system(result)
    return result, tuple(effects)


def _validate_system(system):
    for lane_id, lane in system.lanes.items():
        errors = validate_lane(lane)
        if errors:
            raise ValueError(
                "invalid OpenAMS lane %s: %s"
                % (lane_id, "; ".join(errors))
            )


def _resync_lane(observation, now):
    for group, units in observation.groups.items():
        for unit in units:
            if observation.loaded.get(unit, False):
                return LaneState(
                    mode=LaneMode.LOADED,
                    group=group,
                    unit=unit,
                    since=now,
                )
    return LaneState(since=now)


def _group_of(observation, unit):
    for group, units in observation.groups.items():
        if unit in units:
            return group
    return None


def _reject_load(lane, observation, lane_id):
    if lane.mode != LaneMode.UNLOADED:
        return (
            Settle(
                lane_id,
                OperationResult(
                    False, None, "lane busy (%s)" % lane.mode.value
                ),
            ),
        )
    if any(observation.loaded.values()):
        return (
            Settle(
                lane_id,
                OperationResult(
                    False,
                    None,
                    "filament still detected in a hub on this lane;"
                    " unload it first",
                ),
            ),
        )
    return None


def _begin(
    lane,
    lane_id,
    now,
    owns_liveness,
    kind,
    target,
    *,
    mode,
    group=None,
    calibration=None,
    resume_mode=None
):
    generation = (lane.generation + 1) & 0xFF
    duration = (
        DISCONNECT_BACKSTOP if owns_liveness else ACTION_TIMEOUT
    )
    operation = ActiveOperation(
        kind=kind,
        generation=generation,
        target=target,
        deadline_at=now + duration,
        calibration=calibration,
        resume_mode=resume_mode,
    )
    changed = replace(
        lane,
        mode=mode,
        group=lane.group if group is None else group,
        active=operation,
        generation=generation,
        since=now,
        message=None,
    )
    return changed, (
        StartOperation(lane_id, operation),
        ArmDeadline(lane_id, generation, duration),
    )


def _reduce_lane(
    lane, event, observation, lane_id, firmware_owns_liveness
):
    now = event.observed_at

    if isinstance(event, LoadGroup):
        rejected = _reject_load(lane, observation, lane_id)
        if rejected is not None:
            return lane, rejected
        for unit in observation.groups.get(event.group, ()):
            if observation.ready.get(unit, False):
                changed, effects = _begin(
                    lane,
                    lane_id,
                    now,
                    firmware_owns_liveness,
                    OperationKind.LOAD,
                    unit,
                    mode=LaneMode.LOADING,
                    group=event.group,
                )
                return replace(changed, unit=unit), effects
        return lane, (
            Settle(
                lane_id,
                OperationResult(
                    False,
                    None,
                    "no ready spool in group %s" % event.group,
                ),
            ),
        )

    if isinstance(event, LoadUnit):
        rejected = _reject_load(lane, observation, lane_id)
        if rejected is not None:
            return lane, rejected
        changed, effects = _begin(
            lane,
            lane_id,
            now,
            firmware_owns_liveness,
            OperationKind.LOAD,
            event.unit,
            mode=LaneMode.LOADING,
            group=_group_of(observation, event.unit),
        )
        return replace(changed, unit=event.unit), effects

    if isinstance(event, Unload):
        if lane.mode != LaneMode.LOADED or lane.unit is None:
            return lane, (
                Settle(
                    lane_id,
                    OperationResult(False, None, "nothing loaded"),
                ),
            )
        return _begin(
            lane,
            lane_id,
            now,
            firmware_owns_liveness,
            OperationKind.UNLOAD,
            lane.unit,
            mode=LaneMode.UNLOADING,
        )

    if isinstance(event, Calibrate):
        if lane.mode not in (LaneMode.UNLOADED, LaneMode.LOADED):
            return lane, (
                Settle(
                    lane_id,
                    OperationResult(
                        False, None, "busy, cannot calibrate now"
                    ),
                ),
            )
        return _begin(
            lane,
            lane_id,
            now,
            firmware_owns_liveness,
            OperationKind.CALIBRATE,
            event.unit,
            mode=LaneMode.CALIBRATING,
            calibration=event.kind,
            resume_mode=lane.mode,
        )

    if isinstance(event, Cancel):
        if (
            lane.mode == LaneMode.LOADING
            and lane.active is not None
        ):
            return lane, (
                CancelOperation(
                    lane_id, lane.active.target, lane.active.generation
                ),
            )
        return lane, ()

    if isinstance(event, SetFollowerRequest):
        if lane.unit is None:
            return lane, ()
        changed = replace(
            lane,
            follower=FollowerState(event.enabled, event.direction),
        )
        return changed, (
            SetFollower(
                lane_id,
                lane.unit,
                event.enabled,
                event.direction,
            ),
        )

    if isinstance(event, OperationCompleted):
        if (
            lane.active is None
            or event.generation != lane.active.generation
        ):
            return lane, ()
        return _complete(
            lane, lane_id, event.code, event.value, now, False
        )

    if isinstance(event, DeadlineExpired):
        if (
            lane.active is None
            or event.generation != lane.active.generation
        ):
            return lane, ()
        return _complete(
            lane,
            lane_id,
            ResultCode.ERROR_UNSPECIFIED,
            None,
            now,
            True,
        )

    if isinstance(event, Tick):
        if (
            lane.active is not None
            and now > lane.active.deadline_at
        ):
            return _complete(
                lane,
                lane_id,
                ResultCode.ERROR_UNSPECIFIED,
                None,
                now,
                True,
            )
        if lane.mode == LaneMode.LOADED:
            return _runout_tick(
                lane,
                observation,
                lane_id,
                now,
                firmware_owns_liveness,
            )
        return lane, ()

    return lane, ()


def _finish_deadline(lane_id, lane):
    return CancelDeadline(lane_id, lane.active.generation)


def _complete(lane, lane_id, code, value, now, timed_out):
    operation = lane.active
    if operation is None:
        return lane, ()
    ok = code == ResultCode.SUCCESS
    cancel_deadline = _finish_deadline(lane_id, lane)

    if operation.kind == OperationKind.RELOAD:
        if ok:
            changed = replace(
                lane,
                mode=LaneMode.LOADED,
                unit=operation.target,
                active=None,
                runout=RunoutState(),
                message="next spool loaded",
            )
            return changed, (cancel_deadline,)
        changed = replace(
            lane,
            mode=LaneMode.UNLOADED,
            group=None,
            unit=None,
            follower=FollowerState(),
            active=None,
            runout=RunoutState(),
            message="reload failed",
        )
        reason = (
            "timed out loading next spool"
            if timed_out
            else "failed to load next spool (%s)" % describe_code(code)
        )
        effects = [cancel_deadline]
        if timed_out:
            effects.append(
                CancelOperation(
                    lane_id, operation.target, operation.generation
                )
            )
        effects.append(Pause(lane_id, reason))
        return changed, tuple(effects)

    if operation.kind == OperationKind.LOAD:
        if ok:
            changed = replace(
                lane,
                mode=LaneMode.LOADED,
                active=None,
                message="loaded",
            )
            result = OperationResult(
                True, code, "Spool loaded successfully"
            )
            return changed, (
                cancel_deadline,
                Settle(lane_id, result),
            )
        cancelled = code == ResultCode.CANCEL
        changed = replace(
            lane,
            mode=LaneMode.UNLOADED,
            group=None,
            unit=None,
            follower=FollowerState(),
            active=None,
            runout=RunoutState(),
            message="cancelled" if cancelled else "load failed",
        )
        message = (
            "Spool loading cancelled"
            if cancelled
            else (
                "timed out loading spool"
                if timed_out
                else "Spool loading failed (%s)" % describe_code(code)
            )
        )
        effects = [cancel_deadline]
        if timed_out:
            effects.append(
                CancelOperation(
                    lane_id, operation.target, operation.generation
                )
            )
        if lane.follower.enabled and lane.unit is not None:
            effects.append(
                SetFollower(
                    lane_id,
                    lane.unit,
                    False,
                    lane.follower.direction,
                )
            )
        effects.append(
            Settle(lane_id, OperationResult(False, code, message))
        )
        return changed, tuple(effects)

    if operation.kind == OperationKind.UNLOAD:
        if ok:
            changed = replace(
                lane,
                mode=LaneMode.UNLOADED,
                group=None,
                unit=None,
                follower=FollowerState(),
                active=None,
                message="unloaded",
            )
            return changed, (
                cancel_deadline,
                Settle(
                    lane_id,
                    OperationResult(
                        True, code, "Spool unloaded successfully"
                    ),
                ),
            )
        changed = replace(
            lane,
            mode=LaneMode.LOADED,
            follower=FollowerState(
                False, lane.follower.direction
            ),
            active=None,
            message="unload failed",
        )
        message = (
            "timed out unloading spool"
            if timed_out
            else "Spool unloading failed (%s)" % describe_code(code)
        )
        effects = [
            cancel_deadline,
            SetFollower(
                lane_id, lane.unit, False, Direction.REVERSE
            ),
            Settle(
                lane_id, OperationResult(False, code, message)
            ),
        ]
        return changed, tuple(effects)

    if operation.kind == OperationKind.CALIBRATE:
        resume_mode = operation.resume_mode or LaneMode.UNLOADED
        changed = replace(
            lane,
            mode=resume_mode,
            active=None,
            message="calibrated" if ok else "calibration failed",
        )
        message = (
            "Calibration complete"
            if ok
            else (
                "timed out calibrating"
                if timed_out
                else "Calibration failed (%s)" % describe_code(code)
            )
        )
        return changed, (
            cancel_deadline,
            Settle(
                lane_id,
                OperationResult(ok, code, message, value),
            ),
        )

    return lane, ()


def _runout_tick(
    lane, observation, lane_id, now, firmware_owns_liveness
):
    phase = lane.runout.phase
    if phase == RunoutPhase.IDLE:
        if (
            observation.printing
            and lane.unit is not None
            and not observation.loaded.get(lane.unit, False)
        ):
            return replace(
                lane,
                runout=RunoutState(
                    RunoutPhase.TAIL_BUDGET,
                    observation.extruder_position_mm,
                ),
            ), ()
        return lane, ()

    if lane.unit is None:
        return replace(lane, runout=RunoutState()), ()

    if phase == RunoutPhase.TAIL_BUDGET:
        travelled = (
            observation.extruder_position_mm - lane.runout.origin_mm
        )
        if travelled >= PAUSE_DISTANCE_MM:
            changed = replace(
                lane,
                follower=FollowerState(
                    False, lane.follower.direction
                ),
                runout=RunoutState(
                    RunoutPhase.COASTING,
                    observation.extruder_position_mm,
                ),
            )
            return changed, (
                SetFollower(
                    lane_id,
                    lane.unit,
                    False,
                    Direction.FORWARD,
                ),
            )
        return lane, ()

    path_length = observation.path_length_mm.get(
        lane.unit.node, 0.0
    )
    if path_length <= 0:
        changed = replace(
            lane,
            mode=LaneMode.UNLOADED,
            group=None,
            unit=None,
            follower=FollowerState(),
            runout=RunoutState(),
            message="ptfe_length uncalibrated",
        )
        return changed, (
            Pause(
                lane_id,
                "ptfe_length is not calibrated (0); cannot auto-load"
                " the next spool. Run OAMS_CALIBRATE_PTFE_LENGTH.",
            ),
        )

    consumed = (
        observation.extruder_position_mm - lane.runout.origin_mm
    )
    threshold = (
        consumed + PAUSE_DISTANCE_MM + observation.reload_before_mm
    )
    if threshold <= path_length:
        return lane, ()

    for unit in observation.groups.get(lane.group, ()):
        if unit == lane.unit:
            continue
        if observation.ready.get(unit, False):
            changed, effects = _begin(
                replace(lane, runout=RunoutState()),
                lane_id,
                now,
                firmware_owns_liveness,
                OperationKind.RELOAD,
                unit,
                mode=LaneMode.RELOADING,
            )
            return changed, effects

    changed = replace(
        lane,
        mode=LaneMode.UNLOADED,
        group=None,
        unit=None,
        follower=FollowerState(),
        runout=RunoutState(),
        message="no spare spool",
    )
    return changed, (
        Pause(
            lane_id,
            "filament runout on group %s and no spare spool available"
            % lane.group,
        ),
    )
