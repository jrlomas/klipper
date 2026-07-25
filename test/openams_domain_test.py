import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from helix.openams import model as M
from helix.openams.reducer import reduce
from helix.openams.trace import transition_snapshot


LANE = "fps1"
GROUP = "T0"
UNIT_A = M.UnitRef(1, 0)
UNIT_B = M.UnitRef(1, 3)
GOLDEN = ROOT / "test" / "fixtures" / "openams_reducer_vectors.json"


def observation(
    *,
    position=0.0,
    printing=False,
    loaded=None,
    ready=None,
    path_length=600.0,
    reload_before=0.0
):
    lane = M.LaneObservation(
        extruder_position_mm=position,
        printing=printing,
        loaded={} if loaded is None else loaded,
        ready={} if ready is None else ready,
        groups={GROUP: (UNIT_A, UNIT_B)},
        path_length_mm={1: path_length},
        reload_before_mm=reload_before,
    )
    return M.Observation(lanes={LANE: lane})


def system(lane=None, *, owns_liveness=False):
    return M.SystemState(
        lanes={LANE: lane or M.LaneState()},
        firmware_owns_liveness=owns_liveness,
    )


def run_case(initial, steps):
    current = initial
    trace = []
    for event, world in steps:
        current, effects = reduce(current, event, world)
        trace.append(transition_snapshot(current, effects, LANE))
    return trace


def build_traces():
    traces = {}

    traces["load_success"] = run_case(
        system(),
        (
            (
                M.LoadGroup(LANE, GROUP, 10.0),
                observation(ready={UNIT_A: True}),
            ),
            (
                M.OperationCompleted(
                    LANE, 1, M.ResultCode.SUCCESS, 11.0
                ),
                None,
            ),
        ),
    )

    traces["stale_completion"] = run_case(
        system(),
        (
            (
                M.LoadGroup(LANE, GROUP, 10.0),
                observation(ready={UNIT_A: True}),
            ),
            (
                M.OperationCompleted(
                    LANE, 0, M.ResultCode.SUCCESS, 11.0
                ),
                None,
            ),
        ),
    )

    loaded_reverse = M.LaneState(
        mode=M.LaneMode.LOADED,
        group=GROUP,
        unit=UNIT_A,
        follower=M.FollowerState(True, M.Direction.REVERSE),
    )
    traces["unload_failure"] = run_case(
        system(loaded_reverse),
        (
            (M.Unload(LANE, 20.0), None),
            (
                M.OperationCompleted(
                    LANE, 1, M.ResultCode.ERROR_BUSY, 21.0
                ),
                None,
            ),
        ),
    )

    loaded = M.LaneState(
        mode=M.LaneMode.LOADED,
        group=GROUP,
        unit=UNIT_A,
    )
    traces["calibrate_while_loaded"] = run_case(
        system(loaded),
        (
            (
                M.Calibrate(
                    LANE, UNIT_B, M.CalibrationKind.PTFE, 30.0
                ),
                None,
            ),
            (
                M.OperationCompleted(
                    LANE,
                    1,
                    M.ResultCode.SUCCESS,
                    31.0,
                    value=123,
                ),
                None,
            ),
        ),
    )

    traces["runout_reload_success"] = run_case(
        system(loaded),
        (
            (
                M.Tick(1.0),
                observation(
                    position=1000.0,
                    printing=True,
                    loaded={UNIT_A: False, UNIT_B: True},
                    ready={UNIT_B: True},
                ),
            ),
            (
                M.Tick(2.0),
                observation(
                    position=1060.0,
                    printing=True,
                    loaded={UNIT_A: False, UNIT_B: True},
                    ready={UNIT_B: True},
                ),
            ),
            (
                M.Tick(3.0),
                observation(
                    position=1661.0,
                    printing=True,
                    loaded={UNIT_A: False, UNIT_B: True},
                    ready={UNIT_B: True},
                ),
            ),
            (
                M.OperationCompleted(
                    LANE, 1, M.ResultCode.SUCCESS, 4.0
                ),
                None,
            ),
        ),
    )

    coasting = M.LaneState(
        mode=M.LaneMode.LOADED,
        group=GROUP,
        unit=UNIT_A,
        runout=M.RunoutState(M.RunoutPhase.COASTING, 0.0),
    )
    traces["runout_no_spare"] = run_case(
        system(coasting),
        (
            (
                M.Tick(1.0),
                observation(
                    position=661.0,
                    printing=True,
                    loaded={UNIT_A: False},
                    ready={UNIT_A: True, UNIT_B: False},
                ),
            ),
        ),
    )

    traces["load_timeout"] = run_case(
        system(),
        (
            (
                M.LoadGroup(LANE, GROUP, 0.0),
                observation(ready={UNIT_A: True}),
            ),
            (M.DeadlineExpired(LANE, 1, 121.0), None),
        ),
    )

    traces["resync_loaded"] = run_case(
        system(),
        (
            (
                M.Resync(7.0),
                observation(loaded={UNIT_B: True}),
            ),
        ),
    )

    traces["firmware_liveness_backstop"] = run_case(
        system(owns_liveness=True),
        (
            (
                M.LoadGroup(LANE, GROUP, 0.0),
                observation(ready={UNIT_A: True}),
            ),
        ),
    )

    traces["generation_wrap"] = run_case(
        system(M.LaneState(generation=255)),
        (
            (
                M.LoadGroup(LANE, GROUP, 0.0),
                observation(ready={UNIT_A: True}),
            ),
        ),
    )
    return traces


class OpenAMSDomainTests(unittest.TestCase):
    def test_matches_frozen_transition_corpus(self):
        expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(build_traces(), expected["vectors"])

    def test_rejects_impossible_state_combinations(self):
        invalid = M.LaneState(
            mode=M.LaneMode.UNLOADED,
            unit=UNIT_A,
        )
        self.assertIn(
            "unloaded lane retains material identity",
            M.validate_lane(invalid),
        )

    def test_stale_deadline_cannot_fail_new_operation(self):
        current, _effects = reduce(
            system(),
            M.LoadGroup(LANE, GROUP, 0.0),
            observation(ready={UNIT_A: True}),
        )
        current, effects = reduce(
            current,
            M.DeadlineExpired(LANE, 0, 500.0),
        )
        self.assertEqual(current.lanes[LANE].mode, M.LaneMode.LOADING)
        self.assertEqual(effects, ())


if __name__ == "__main__":
    if os.environ.get("HELIX_DUMP_OPENAMS_GOLDEN") == "1":
        print(
            json.dumps(
                {
                    "schema": 1,
                    "oracle": {
                        "repository": "klipper_openams",
                        "commit": "0d43ae1",
                        "tests": "193 passed",
                        "normalization":
                            "hierarchical OpenAMS semantic trace v1",
                    },
                    "vectors": build_traces(),
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        unittest.main()
