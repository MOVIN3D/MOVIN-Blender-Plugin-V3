# Copyright 2025 MOVIN. All Rights Reserved.

"""Reintroduce each known trap and confirm the test suite actually fails.

    python tests/mutation_check.py

A test that cannot fail is not protecting anything. Every mutation below is a bug
that was really shipped and really had to be found the hard way, mostly on the
Unreal side; the suite is only worth keeping if putting them back turns it red.

Three of these survived the first time this was run, which is how two genuine
holes in the tests were found - the pelvis exclusion was never checked as being
*derived* from observation, and the ranking and signature fixes were each masking
the other.
"""
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(r"C:\Workspace\MOVIN-Blender-Plugin-V3\tests")))
import test_skeleton_diagnostics as suite  # installs the bpy stubs

movin = suite.movin


def run(names):
    loader = unittest.TestLoader()
    tests = unittest.TestSuite(loader.loadTestsFromName(n, suite) for n in names)
    result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(tests)
    return result


def check(label, patch, restore, expect_failing):
    patch()
    try:
        result = run(expect_failing)
        failed = set()
        for case, _ in list(result.failures) + list(result.errors):
            failed.add(case.id().rsplit(".", 1)[-1])
    finally:
        restore()

    missed = [n for n in expect_failing if n.rsplit(".", 1)[-1] not in failed]
    status = "CAUGHT" if not missed else "MISSED"
    print("%-6s %s" % (status, label))
    for name in missed:
        print("         test did not fail: %s" % name)
    return not missed


ok = True

# Trap 1: infer world-motion bones from the hierarchy instead of by observation.
# Modelled as the pelvis simply not being excluded.
_real_find = movin.find_world_motion_bones
ok &= check(
    "trap 1: pelvis not excluded (hierarchy guess misses it)",
    lambda: setattr(movin, "find_world_motion_bones", lambda *a, **k: set()),
    lambda: setattr(movin, "find_world_motion_bones", _real_find),
    ["SignatureTests.test_the_pelvis_is_excluded_by_observation_not_by_configuration"],
)

# Trap 1b: infer world movement from the hierarchy - "skip the root and what
# hangs off it". On MOVINman's stream the pelvis is not the root, so it slips
# through; on the imported .fbx it is, so the thighs and spine get thrown away.
def _hierarchy_guess(bone_names, counts, frames):
    return set(bone_names[:1])


ok &= check(
    "trap 1b: world movement inferred from hierarchy position",
    lambda: setattr(movin, "find_world_motion_bones", _hierarchy_guess),
    lambda: setattr(movin, "find_world_motion_bones", _real_find),
    ["WorldMotionTests.test_nothing_is_claimed_before_enough_frames",
     "SignatureTests.test_the_pelvis_is_excluded_by_observation_not_by_configuration"],
)

# Trap 2: judge world movement by min/max range rather than a change count, so a
# one-off recalibration is mistaken for movement and silenced.
def _range_based(bone_names, counts, frames):
    if frames < movin.SKELETON_MIN_FRAMES_FOR_MOTION_CHECK:
        return set()
    return set(bone_names[i] for i in range(min(len(bone_names), len(counts)))
               if counts[i] > 0)


ok &= check(
    "trap 2: recalibration mistaken for world movement",
    lambda: setattr(movin, "find_world_motion_bones", _range_based),
    lambda: setattr(movin, "find_world_motion_bones", _real_find),
    ["WorldMotionTests.test_a_one_off_recalibration_is_not_world_movement"],
)

# Trap 3a: build the signature from the ranked order instead of sorting by name.
_real_signature = movin.build_report_signature


def _ranked_signature(target_name, report):
    if not report.deviations:
        return "%s|match" % target_name
    return target_name + "|" + "|".join(
        "%s=%s" % (d.bone_name, d.display_ratio) for d in report.deviations)


ok &= check(
    "trap 3a: signature built from ranked order",
    lambda: setattr(movin, "build_report_signature", _ranked_signature),
    lambda: setattr(movin, "build_report_signature", _real_signature),
    ["SignatureTests.test_the_signature_ignores_the_order_of_the_deviation_list"],
)

# Trap 3b: build the signature from raw ratios, so invisible jitter re-notifies.
def _raw_signature(target_name, report):
    if not report.deviations:
        return "%s|match" % target_name
    return target_name + "|" + "|".join(
        sorted("%s=%r" % (d.bone_name, d.ratio) for d in report.deviations))


ok &= check(
    "trap 3b: signature built from raw ratios",
    lambda: setattr(movin, "build_report_signature", _raw_signature),
    lambda: setattr(movin, "build_report_signature", _real_signature),
    ["SignatureTests.test_jitter_below_display_precision_does_not_renotify"],
)

# Trap 3c: rank on the raw magnitude with no name tiebreak - the original bug.
# Near-tied symmetric pairs swap places between scans, so the printed text churns
# even though every figure the user can see is identical.
_real_rank_key = movin._deviation_rank_key
ok &= check(
    "trap 3c: ranking on raw magnitude churns the printed order",
    lambda: setattr(movin, "_deviation_rank_key",
                    lambda d: -abs(d.ratio - 1.0)),
    lambda: setattr(movin, "_deviation_rank_key", _real_rank_key),
    ["SignatureTests.test_symmetric_bone_order_does_not_change_the_signature"],
)

# Trap 3d: the two together, which is what shipped first: raw-float ranking and a
# signature derived from that order.
def _both():
    movin._deviation_rank_key = lambda d: -abs(d.ratio - 1.0)
    movin.build_report_signature = _ranked_signature


def _neither():
    movin._deviation_rank_key = _real_rank_key
    movin.build_report_signature = _real_signature


ok &= check(
    "trap 3d: raw-float ranking feeding a ranked-order signature",
    _both, _neither,
    ["SignatureTests.test_symmetric_bone_order_does_not_change_the_signature"],
)

# Trap 5: diagnose every subject, including already-retargeted Character streams.
_real_is_actor = movin.is_actor_subject
ok &= check(
    "trap 5: every subject diagnosed, not only Actor",
    lambda: setattr(movin, "is_actor_subject", lambda name: bool(name)),
    lambda: setattr(movin, "is_actor_subject", _real_is_actor),
    ["ActorSubjectTests.test_only_actor_streams_are_diagnosed",
     "ObservationTests.test_character_streams_are_never_observed"],
)

# Blender-specific: drop the unit plausibility guard.
_real_range = movin.SKELETON_PLAUSIBLE_MEDIAN_RATIO
ok &= check(
    "units: plausibility guard removed",
    lambda: setattr(movin, "SKELETON_PLAUSIBLE_MEDIAN_RATIO", (0.0, float("inf"))),
    lambda: setattr(movin, "SKELETON_PLAUSIBLE_MEDIAN_RATIO", _real_range),
    ["UnitTests.test_centimetre_rest_pose_against_metre_stream_is_caught",
     "UnitTests.test_centimetre_stream_against_metre_rest_pose_is_caught"],
)

# And the whole suite must still be green once everything is restored.
final = run(["ActorSubjectTests", "WorldMotionTests", "ObservationTests",
             "CompareTests", "SignatureTests", "UnitTests", "MessageTests"])
print("\nsuite after restore: ran %d, failures %d, errors %d"
      % (final.testsRun, len(final.failures), len(final.errors)))
ok &= final.wasSuccessful()

print("\n%s" % ("ALL MUTATIONS CAUGHT" if ok else "SOME MUTATIONS SURVIVED"))
sys.exit(0 if ok else 1)
