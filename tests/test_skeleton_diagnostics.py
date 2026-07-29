# Copyright 2025 MOVIN. All Rights Reserved.

"""Unit tests for the Skeleton Calibration Offset comparison.

The add-on ships as a single .py file so it can be installed with
Edit > Preferences > Add-ons > Install..., which means the comparison logic
lives in the same module as the Blender integration. Everything under test here
is pure Python, so stubbing bpy and mathutils is enough to import it and run
outside Blender:

    python tests/test_skeleton_diagnostics.py

The failure mode this feature has is repetition - the notice coming back every
couple of seconds when nothing the user can see has changed. Most of these tests
exist to pin that down.
"""

import math
import sys
import types
import unittest
from pathlib import Path

ADDON_DIR = Path(__file__).resolve().parent.parent / "addon"


def _install_blender_stubs():
    """Enough of bpy and mathutils to import the add-on module.

    Only module import needs satisfying: the property declarations evaluated
    while the PropertyGroup class body runs, and the base classes the operators
    and panel derive from. Nothing under test touches any of it.
    """
    bpy = types.ModuleType("bpy")

    bpy_props = types.ModuleType("bpy.props")
    for name in ("PointerProperty", "StringProperty", "IntProperty",
                 "BoolProperty", "FloatProperty", "EnumProperty"):
        setattr(bpy_props, name, lambda *args, **kwargs: None)

    bpy_types = types.ModuleType("bpy.types")
    for name in ("PropertyGroup", "Panel", "Operator"):
        setattr(bpy_types, name, type(name, (object,), {}))

    bpy.props = bpy_props
    bpy.types = bpy_types
    bpy.data = None
    bpy.utils = types.SimpleNamespace()
    bpy.app = types.SimpleNamespace(timers=None)
    bpy.context = None

    mathutils = types.ModuleType("mathutils")
    mathutils.Quaternion = type("Quaternion", (object,), {})
    mathutils.Vector = type("Vector", (object,), {})

    sys.modules.setdefault("bpy", bpy)
    sys.modules.setdefault("bpy.props", bpy_props)
    sys.modules.setdefault("bpy.types", bpy_types)
    sys.modules.setdefault("mathutils", mathutils)


_install_blender_stubs()
sys.path.insert(0, str(ADDON_DIR))

import movin_blender_plugin as movin  # noqa: E402


# -----------------------------------------------------------------------------
# Fixtures
#
# Rest offsets are the real figures read out of samples/blend/MOVINman_V3_Sample
# (metres, offset of each bone's origin from its parent's), and the ratios are
# the ones measured in a live Unreal session against the same character. So this
# fixture is the shape the feature was actually built against, not a toy.
# -----------------------------------------------------------------------------

ARMATURE = "G5_MVMAN"

#: name -> (rest offset in metres, streamed/rest ratio measured live)
MOVINMAN = {
    "Hips":             (0.90870, None),   # world position, not a length
    "Spine":            (0.10057, 1.00),
    "Neck":             (0.09093, 1.42),
    "Neck1":            (0.08587, 0.52),
    "Head":             (0.04687, 0.96),
    "LeftUpLeg":        (0.10052, 1.20),
    "RightUpLeg":       (0.10052, 1.20),
    "LeftLeg":          (0.43978, 0.93),
    "RightLeg":         (0.43978, 0.93),
    "LeftFoot":         (0.41723, 0.93),
    "RightFoot":        (0.41723, 0.93),
    "LeftArm":          (0.16468, 1.12),
    "RightArm":         (0.16468, 1.12),
    "LeftForeArm":      (0.28900, 0.91),
    "RightForeArm":     (0.28900, 0.91),
    "LeftHand":         (0.24038, 0.91),
    "RightHand":        (0.24038, 0.91),
    "LeftHandIndex1":   (0.10850, None),   # a finger: in the rig, never streamed
}

#: MOVIN Studio's skeleton carries a root bone above the pelvis that the imported
#: .fbx does not have, so the two sides disagree about where the pelvis sits in
#: the hierarchy. Any rule phrased as "skip the root and what hangs off it"
#: therefore misses on one side or the other, which is why world movement is
#: worked out by watching the stream instead.
STREAM_ONLY_ROOT = "RootBone"

#: Bone order as it arrives in the stream. Fingers are absent, as they are live.
STREAMED_ORDER = [
    STREAM_ONLY_ROOT,
    "Hips", "Spine", "Neck", "Neck1", "Head",
    "LeftUpLeg", "RightUpLeg", "LeftLeg", "RightLeg", "LeftFoot", "RightFoot",
    "LeftArm", "RightArm", "LeftForeArm", "RightForeArm", "LeftHand", "RightHand",
]

#: Streamed bones that carry a real length and exist in the rig - everything the
#: comparison should actually look at.
COMPARABLE = [name for name in STREAMED_ORDER
              if name in MOVINMAN and name != "Hips"]

#: What find_world_motion_bones() reports for a normal performance.
PELVIS_EXCLUSION = frozenset(("Hips",))

#: Hips translation is a world position, so it lands anywhere in this span.
HIPS_LOW = 0.43
HIPS_HIGH = 2.38


def rest_bones(scale=1.0):
    """The armature side of the comparison. `scale` fakes a unit error."""
    return [movin.RestBone(name, MOVINMAN[name][0] * scale)
            for name in sorted(MOVINMAN)]


def streamed_lengths(hips=HIPS_LOW, overrides=None, scale=1.0):
    """The stream side, built from the measured ratios.

    `overrides` replaces individual lengths outright, for recalibrations and
    far-decimal jitter. `scale` fakes a unit error.
    """
    overrides = overrides or {}
    lengths = []
    for name in STREAMED_ORDER:
        if name in overrides:
            lengths.append(overrides[name] * scale)
        elif name == STREAM_ONLY_ROOT:
            lengths.append(0.0)  # sits at the origin, so it holds still
        elif name == "Hips":
            lengths.append(hips * scale)
        else:
            rest, ratio = MOVINMAN[name]
            lengths.append(rest * ratio * scale)
    return lengths


def compare(hips=HIPS_LOW, overrides=None, rest_scale=1.0, streamed_scale=1.0,
            excluded=PELVIS_EXCLUSION):
    return movin.compare_bone_lengths(
        rest_bones(rest_scale),
        STREAMED_ORDER,
        streamed_lengths(hips=hips, overrides=overrides, scale=streamed_scale),
        excluded,
    )


def signature(report, target=ARMATURE):
    return movin.build_report_signature(target, report)


def deviation(report, bone_name):
    for item in report.deviations:
        if item.bone_name == bone_name:
            return item
    return None


# -----------------------------------------------------------------------------
# Which streams get diagnosed
# -----------------------------------------------------------------------------

class ActorSubjectTests(unittest.TestCase):
    def test_only_actor_streams_are_diagnosed(self):
        # Character streams are already retargeted onto the loaded .fbx by MOVIN
        # Studio, so their lengths match and there is nothing to explain.
        self.assertTrue(movin.is_actor_subject("MOVINMan"))
        self.assertTrue(movin.is_actor_subject("MOVINman"))
        self.assertTrue(movin.is_actor_subject("  MOVINMan  "))
        self.assertFalse(movin.is_actor_subject("Ch14_nonPBR"))
        self.assertFalse(movin.is_actor_subject(""))
        self.assertFalse(movin.is_actor_subject(None))


# -----------------------------------------------------------------------------
# Telling world movement apart from a bone length
# -----------------------------------------------------------------------------

class WorldMotionTests(unittest.TestCase):
    def test_a_bone_that_moves_every_frame_is_world_movement(self):
        # Decided by watching the stream, not by reading the hierarchy: MOVIN
        # Studio's skeleton has a root above the pelvis and the imported .fbx
        # does not, so no hierarchy rule holds on both sides.
        detected = movin.find_world_motion_bones(
            ["Hips", "Spine", "Neck", "Head"], [590, 0, 0, 0], 600)
        self.assertEqual(detected, {"Hips"})

    def test_a_one_off_recalibration_is_not_world_movement(self):
        # A recalibration changes a length once and holds it. Silencing the bone
        # would suppress exactly the report the user needs. A min/max range test
        # could not tell this apart from movement; a change count can.
        detected = movin.find_world_motion_bones(
            ["Hips", "Spine", "Neck", "Head"], [590, 1, 0, 0], 600)
        self.assertEqual(detected, {"Hips"})

    def test_nothing_is_claimed_before_enough_frames(self):
        # Below the warm-up nothing is concluded, and nothing is shown.
        self.assertEqual(
            movin.find_world_motion_bones(["Hips", "Spine"], [5, 0], 5), set())
        self.assertEqual(
            movin.find_world_motion_bones(
                ["Hips", "Spine"], [20, 0],
                movin.SKELETON_MIN_FRAMES_FOR_MOTION_CHECK - 1),
            set())

    def test_short_count_array_is_not_read_past_the_end(self):
        detected = movin.find_world_motion_bones(
            ["Hips", "Spine", "Neck"], [590], 600)
        self.assertEqual(detected, {"Hips"})


class ObservationTests(unittest.TestCase):
    """The receiver-thread half: counting how often each length changes."""

    @staticmethod
    def _frame(hips_height, overrides=None):
        overrides = overrides or {}
        bones = []
        for name in STREAMED_ORDER:
            if name in overrides:
                length = overrides[name]
            elif name == STREAM_ONLY_ROOT:
                length = 0.0
            elif name == "Hips":
                length = hips_height
            else:
                rest, ratio = MOVINMAN[name]
                length = rest * ratio
            # Direction is irrelevant - only |p| is compared - so put it all on
            # one axis. This mirrors what the OSC parser hands over: metres.
            bones.append({"bone_name": name, "p": (0.0, length, 0.0)})
        return bones

    def _observe(self, runtime, frame_count, subject="MOVINMan"):
        for index in range(frame_count):
            # A performer's pelvis is somewhere different on every frame.
            hips = HIPS_LOW + (HIPS_HIGH - HIPS_LOW) * (index % 7) / 6.0
            runtime.note_skeleton_locked(subject, self._frame(hips))

    def test_only_the_pelvis_is_flagged_after_a_performance(self):
        runtime = movin.MOVINRuntime()
        self._observe(runtime, 120)

        self.assertEqual(runtime.skeleton_frames_observed, 120)
        detected = movin.find_world_motion_bones(
            runtime.skeleton_bone_names,
            runtime.skeleton_length_change_counts,
            runtime.skeleton_frames_observed)
        self.assertEqual(detected, {"Hips"})

    def test_character_streams_are_never_observed(self):
        runtime = movin.MOVINRuntime()
        self._observe(runtime, 60, subject="Ch14_nonPBR")
        self.assertEqual(runtime.skeleton_frames_observed, 0)
        self.assertEqual(runtime.skeleton_bone_names, [])

    def test_a_recalibration_mid_session_stays_out_of_the_exclusion_set(self):
        runtime = movin.MOVINRuntime()
        self._observe(runtime, 60)
        # One length changes and then holds for the rest of the session.
        recalibrated = {"Neck1": MOVINMAN["Neck1"][0] * 0.61}
        for index in range(60):
            hips = HIPS_LOW + (HIPS_HIGH - HIPS_LOW) * (index % 7) / 6.0
            runtime.note_skeleton_locked("MOVINMan", self._frame(hips, recalibrated))

        detected = movin.find_world_motion_bones(
            runtime.skeleton_bone_names,
            runtime.skeleton_length_change_counts,
            runtime.skeleton_frames_observed)
        self.assertEqual(detected, {"Hips"})
        self.assertEqual(runtime.skeleton_length_change_counts[
            runtime.skeleton_bone_names.index("Neck1")], 1)

    def test_a_new_bone_list_restarts_the_warm_up(self):
        runtime = movin.MOVINRuntime()
        self._observe(runtime, 60)
        runtime.skeleton_notified_signatures[("MOVINMan", ARMATURE)] = "stale"

        runtime.note_skeleton_locked("MOVINMan", [
            {"bone_name": "Hips", "p": (0.0, 0.9, 0.0)},
            {"bone_name": "Spine", "p": (0.0, 0.1, 0.0)},
        ])
        self.assertEqual(runtime.skeleton_frames_observed, 1)
        self.assertEqual(runtime.skeleton_bone_names, ["Hips", "Spine"])
        self.assertEqual(runtime.skeleton_notified_signatures, {})
        self.assertIsNone(runtime.skeleton_banner)


# -----------------------------------------------------------------------------
# The comparison itself
# -----------------------------------------------------------------------------

class CompareTests(unittest.TestCase):
    def test_matching_lengths_report_nothing(self):
        # Character streaming: MOVIN Studio has already retargeted onto this
        # exact rig, so every length matches and there is nothing to say.
        matched = dict((name, MOVINMAN[name][0]) for name in COMPARABLE)
        report = compare(overrides=matched)

        self.assertFalse(report.has_deviation())
        self.assertEqual(signature(report), ARMATURE + "|match")

    def test_measured_session_figures(self):
        report = compare()

        self.assertTrue(report.has_deviation())
        # 18 bones in the rig, less the pelvis (world movement) and the finger
        # (never streamed).
        self.assertEqual(report.compared_bone_count, 16)
        # Spine matches to two decimals, so it is not worth mentioning.
        self.assertIsNone(deviation(report, "Spine"))
        self.assertEqual(len(report.deviations), 15)

        self.assertEqual(deviation(report, "Neck1").display_ratio, "0.52")
        self.assertEqual(deviation(report, "Neck").display_ratio, "1.42")
        self.assertEqual(deviation(report, "LeftUpLeg").display_ratio, "1.20")
        self.assertEqual(deviation(report, "LeftArm").display_ratio, "1.12")
        self.assertEqual(deviation(report, "LeftForeArm").display_ratio, "0.91")
        self.assertEqual(deviation(report, "LeftFoot").display_ratio, "0.93")
        self.assertEqual(deviation(report, "Head").display_ratio, "0.96")

    def test_worst_offenders_rank_first(self):
        # The first six are what the notice actually prints, so their order is
        # the order the user reads.
        report = compare()
        self.assertEqual(
            [d.bone_name for d in report.deviations[:6]],
            ["Neck1", "Neck", "LeftUpLeg", "RightUpLeg", "LeftArm", "RightArm"])

    def test_unstreamed_bones_are_skipped(self):
        # The rig has fingers and twist bones the stream never carries. They must
        # be skipped, not reported as infinitely wrong.
        report = compare()
        self.assertIsNone(deviation(report, "LeftHandIndex1"))
        self.assertNotIn("LeftHandIndex1", signature(report))

    def test_bones_the_rig_does_not_have_are_skipped(self):
        # The other direction: MOVIN Studio's root bone has no counterpart in the
        # imported .fbx.
        report = compare()
        self.assertIsNone(deviation(report, STREAM_ONLY_ROOT))
        self.assertNotIn(STREAM_ONLY_ROOT, signature(report))

    def test_zero_length_rest_bones_are_skipped(self):
        report = movin.compare_bone_lengths(
            [movin.RestBone("RootBone", 0.0), movin.RestBone("Spine", 0.10057)],
            ["RootBone", "Spine"], [0.0, 0.10057])
        self.assertEqual(report.compared_bone_count, 1)

    def test_malformed_and_empty_frames_are_survivable(self):
        # A frame whose length array is shorter than its name array must not be
        # read past the end. Only Spine is both present and comparable here.
        report = movin.compare_bone_lengths(
            rest_bones(), STREAMED_ORDER, [0.0, HIPS_LOW, MOVINMAN["Spine"][0]],
            PELVIS_EXCLUSION)
        self.assertEqual(report.compared_bone_count, 1)

        self.assertEqual(
            movin.compare_bone_lengths(rest_bones(), [], []).compared_bone_count, 0)
        self.assertEqual(
            movin.compare_bone_lengths([], STREAMED_ORDER, streamed_lengths()
                                       ).compared_bone_count, 0)


# -----------------------------------------------------------------------------
# Repetition - the only failure mode this feature has
# -----------------------------------------------------------------------------

class SignatureTests(unittest.TestCase):
    def test_the_pelvis_is_excluded_by_observation_not_by_configuration(self):
        # The whole chain, the way the add-on runs it: watch a performance, work
        # out from the observation alone which bones carry world movement, then
        # compare. Nothing here is told in advance that the pelvis is special -
        # if that inference breaks, the pelvis lands in the report and its
        # "length" changes on every scan.
        runtime = movin.MOVINRuntime()
        observation = ObservationTests()
        observation._observe(runtime, 200)

        excluded = movin.find_world_motion_bones(
            runtime.skeleton_bone_names,
            runtime.skeleton_length_change_counts,
            runtime.skeleton_frames_observed)
        self.assertEqual(excluded, {"Hips"})

        low = compare(hips=HIPS_LOW, excluded=excluded)
        high = compare(hips=HIPS_HIGH, excluded=excluded)
        self.assertIsNone(deviation(low, "Hips"))
        self.assertEqual(signature(high), signature(low))

    def test_the_signature_ignores_the_order_of_the_deviation_list(self):
        # The signature answers "is this the same set of figures". The order the
        # notice happens to list them in is not part of that, so deriving it from
        # the ranked order makes a reshuffle read as a fresh report.
        report = compare()
        before = signature(report)
        report.deviations.reverse()
        self.assertEqual(signature(report), before)

    def test_a_moving_pelvis_does_not_change_the_signature(self):
        # The pelvis translation is a world position, so it differs on every
        # frame of a performance. Treating that as a recalibration is what made
        # the notice re-appear every couple of seconds.
        low = compare(hips=HIPS_LOW)
        high = compare(hips=HIPS_HIGH)
        self.assertEqual(signature(high), signature(low))
        self.assertEqual(
            movin.format_report("MOVINMan", ARMATURE, high),
            movin.format_report("MOVINMan", ARMATURE, low))

    def test_jitter_below_display_precision_does_not_renotify(self):
        # If the user cannot see a change, it is not a change.
        jittered = dict(
            (name, MOVINMAN[name][0] * MOVINMAN[name][1] * 1.0004)
            for name in COMPARABLE)
        self.assertEqual(
            signature(compare(overrides=jittered)), signature(compare()))

    def test_symmetric_bone_order_does_not_change_the_signature(self):
        # A performer's left and right sides calibrate to figures that agree to
        # two decimals and differ only in the far ones, so whichever of a pair
        # ranks higher can flip between scans. Nothing the user could see has
        # changed, but a signature built from the ranked order would differ -
        # which is enough to re-raise the notice every two seconds.
        pairs = ("UpLeg", "Leg", "Foot", "Arm", "ForeArm", "Hand")
        forward, reversed_ = {}, {}
        for index, stem in enumerate(pairs):
            nudge = 1.0 + (index + 1) * 1e-6
            rest, ratio = MOVINMAN["Left" + stem]
            forward["Left" + stem] = rest * ratio * nudge
            forward["Right" + stem] = rest * ratio
            reversed_["Left" + stem] = rest * ratio
            reversed_["Right" + stem] = rest * ratio * nudge

        first = compare(overrides=forward)
        second = compare(overrides=reversed_)

        self.assertEqual(signature(second), signature(first))
        # The printed order has to hold still too, or the notice text churns
        # even when nothing did.
        self.assertEqual(
            movin.format_report("MOVINMan", ARMATURE, second),
            movin.format_report("MOVINMan", ARMATURE, first))
        # Ranking still works: ties break by name, left before right.
        self.assertEqual(first.deviations[2].bone_name, "LeftUpLeg")
        self.assertEqual(first.deviations[3].bone_name, "RightUpLeg")

    def test_a_real_recalibration_changes_the_signature(self):
        # The user gets the corrected figures. This is the one case that must
        # re-raise the notice.
        recalibrated = {"Neck1": MOVINMAN["Neck1"][0] * 0.61}
        self.assertNotEqual(
            signature(compare(overrides=recalibrated)), signature(compare()))

    def test_a_different_armature_changes_the_signature(self):
        report = compare()
        self.assertNotEqual(signature(report, "Ch14"), signature(report, ARMATURE))

    def test_notice_state_is_kept_per_subject_and_armature(self):
        # One subject can drive more than one armature over a session. Holding a
        # single signature per subject made the two overwrite each other's record
        # and re-notify on every scan.
        notified = {}

        def would_notify(subject, target, report):
            key = (subject, target)
            current = movin.build_report_signature(target, report)
            if notified.get(key) == current:
                return False
            notified[key] = current
            return True

        first = compare(hips=HIPS_LOW)
        second = movin.compare_bone_lengths(
            rest_bones(), STREAMED_ORDER,
            streamed_lengths(overrides={"Neck1": MOVINMAN["Neck1"][0] * 0.61}),
            PELVIS_EXCLUSION)

        self.assertTrue(would_notify("MOVINMan", ARMATURE, first))
        self.assertTrue(would_notify("MOVINMan", "Ch14", second))
        for _ in range(5):
            # The pelvis keeps moving; neither armature may speak again.
            self.assertFalse(would_notify(
                "MOVINMan", ARMATURE, compare(hips=HIPS_HIGH)))
            self.assertFalse(would_notify("MOVINMan", "Ch14", second))


# -----------------------------------------------------------------------------
# Units - the Blender-specific hazard
# -----------------------------------------------------------------------------

class UnitTests(unittest.TestCase):
    def test_centimetre_rest_pose_against_metre_stream_is_caught(self):
        # samples/blend/Ch14_Sample has its armature object at scale 0.01 with
        # bone data 100x larger, which is what an .fbx import from a centimetre
        # DCC leaves behind. Comparing that raw would report every bone at 100x.
        report = compare(rest_scale=100.0)

        self.assertTrue(report.unit_mismatch)
        self.assertFalse(report.has_deviation())
        self.assertIn("unit-mismatch", signature(report))

    def test_centimetre_stream_against_metre_rest_pose_is_caught(self):
        report = compare(streamed_scale=100.0)
        self.assertTrue(report.unit_mismatch)
        self.assertFalse(report.has_deviation())

    def test_normalising_both_sides_restores_the_real_report(self):
        # Scaling both sides together is what collect_rest_bones() achieves by
        # going through matrix_world: the ratios, and so the notice, are
        # unchanged.
        scaled = compare(rest_scale=100.0, streamed_scale=100.0)
        self.assertFalse(scaled.unit_mismatch)
        self.assertEqual(signature(scaled), signature(compare()))

    def test_a_genuinely_small_character_is_not_mistaken_for_a_unit_error(self):
        # Half-scale is a real character, not a unit slip, and must still report.
        report = compare(rest_scale=0.5)
        self.assertFalse(report.unit_mismatch)
        self.assertTrue(report.has_deviation())


# -----------------------------------------------------------------------------
# The message
# -----------------------------------------------------------------------------

class MessageTests(unittest.TestCase):
    def test_it_names_the_bones_and_denies_being_a_bug(self):
        # This is the whole feature. Naming the bone the user is squinting at,
        # with its ratio, is what turns "the plugin is broken" into "the plugin
        # already knows, so it is meant to happen".
        text = movin.format_report("MOVINMan", ARMATURE, compare())

        self.assertIn("MOVINMan", text)
        self.assertIn(ARMATURE, text)
        self.assertIn("Neck1 0.52x", text)
        self.assertIn("LeftUpLeg 1.20x", text)
        self.assertIn("not a plugin error", text)
        # A different path for a different goal, rather than a fix that cannot
        # exist.
        self.assertIn("Character", text)
        self.assertIn("retarget", text)

    def test_the_list_is_truncated_with_a_count_of_the_rest(self):
        report = compare()
        text = movin.format_report("MOVINMan", ARMATURE, report)
        hidden = len(report.deviations) - movin.SKELETON_MAX_REPORTED_BONES
        self.assertIn("(+%d more)" % hidden, text)
        self.assertNotIn("Head 0.96x", text)

    def test_lines_fit_a_side_panel(self):
        for line in movin.format_report_lines("MOVINMan", ARMATURE, compare()):
            self.assertLessEqual(len(line), 64, line)

    def test_it_states_the_consequence_the_user_is_looking_at(self):
        # Streamed local scale is applied to every pose bone on every frame, so
        # the mesh really does change shape at the listed joints. Saying anything
        # softer would not match what the user is seeing.
        text = movin.format_report("MOVINMan", ARMATURE, compare())
        self.assertIn("changes shape", text)


# -----------------------------------------------------------------------------
# Applying the calibrated offset
# -----------------------------------------------------------------------------

IDENTITY_AXES = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))

#: A bone tilted about 1 degree relative to its parent - the mildest case that
#: still exposes a missing rotation. Real rigs are far worse: the bones of
#: MOVINman_V3_Sample average 12 degrees off the armature axes with thumbs at 60,
#: and Ch14_Sample's average 116.
TILTED_AXES = ((1.0, 0.0, 0.0), (0.0, 0.9998, 0.0183), (0.0, -0.0183, 0.9998))


class RestRelativeLocationTests(unittest.TestCase):
    """Placing a bone's origin at the streamed offset.

    Everything is in the parent bone's rest frame. Reading these as armature-space
    vectors projected onto the bone's own axes is the mistake that shipped once:
    it computes a different quantity that happens to look right where a bone sits
    near the armature axes, and mangles fingers where it does not.

    Also worth recording: that version was checked against a fixture built by
    inverting the add-on's own conversion, which is circular - it proves the code
    agrees with itself, not that the frame is the stream's. The checks that
    actually caught it run in Blender against its own pose evaluator, asserting a
    frame-independent fact: stream an offset at 1.20x and the joint must sit 1.20x
    as far from its parent.
    """

    def test_equal_vectors_produce_no_offset(self):
        # A Character stream is retargeted onto this same .fbx, so its offsets
        # equal the rest offsets and nothing may be displaced.
        rest = (0.1647, 0.0, -0.0263)
        for axes in (IDENTITY_AXES, TILTED_AXES):
            location = movin.rest_relative_location(rest, rest, axes)
            for component in location:
                self.assertAlmostEqual(component, 0.0, places=9)

    def test_the_offset_is_the_difference_from_the_rest_pose(self):
        # A thigh calibrated 20% wider than the rig's own.
        rest = (0.10052, 0.0, 0.0)
        streamed = (0.10052 * 1.20, 0.0, 0.0)
        location = movin.rest_relative_location(streamed, rest, IDENTITY_AXES)
        self.assertAlmostEqual(location[0], 0.10052 * 0.20, places=9)
        self.assertAlmostEqual(location[1], 0.0, places=9)
        self.assertAlmostEqual(location[2], 0.0, places=9)

    def test_the_difference_is_rotated_into_the_supplied_frame(self):
        # The location channel runs along the axes handed in, so the difference
        # has to be rotated into them. Skipping it drops a component outright.
        rest = (0.0, 0.08023, 0.0)
        streamed = (0.0, 0.09000, 0.0)
        naive = streamed[1] - rest[1]

        location = movin.rest_relative_location(streamed, rest, TILTED_AXES)
        self.assertAlmostEqual(location[1], naive * 0.9998, places=9)
        # The component the naive version drops entirely.
        self.assertAlmostEqual(location[2], naive * -0.0183, places=9)
        self.assertNotAlmostEqual(location[2], 0.0, places=6)

    def test_no_baseline_is_captured_from_the_stream(self):
        # Whatever arrived first cannot influence the result: the same streamed
        # value always yields the same location, in any order.
        rest = (0.2404, 0.0, 0.0)
        first = movin.rest_relative_location((0.2200, 0.0, 0.0), rest, IDENTITY_AXES)
        movin.rest_relative_location((9.9999, 0.0, 0.0), rest, IDENTITY_AXES)
        again = movin.rest_relative_location((0.2200, 0.0, 0.0), rest, IDENTITY_AXES)
        self.assertEqual(again, first)


class HipsLocationTests(unittest.TestCase):
    """The hips takes the same code path, with the performer's standing height as
    its reference instead of a parent-relative offset."""

    PERFORMER_HIPS_HEIGHT = 0.87   # metres; the property stores this negated

    def hips_location(self, streamed_metres, units_per_metre, axes=IDENTITY_AXES):
        hips_y_offset = -self.PERFORMER_HIPS_HEIGHT
        return movin.rest_relative_location(
            tuple(component * units_per_metre for component in streamed_metres),
            (0.0, -hips_y_offset * units_per_metre, 0.0),
            axes)

    def test_standing_still_leaves_the_armature_at_its_own_hips_height(self):
        # At the performer's standing height the hips must not be displaced, so
        # the character sits at whatever height its own rest pose puts it - not
        # at the performer's.
        for units_per_metre in (1.0, 100.0):
            location = self.hips_location(
                (0.0, self.PERFORMER_HIPS_HEIGHT, 0.0), units_per_metre)
            for component in location:
                self.assertAlmostEqual(component, 0.0, places=9)

    def test_only_the_movement_away_from_standing_is_transferred(self):
        # A 12 cm crouch is a 12 cm crouch on any rig, in that rig's units.
        crouch = self.PERFORMER_HIPS_HEIGHT - 0.12

        metre_rig = self.hips_location((0.0, crouch, 0.0), 1.0)
        self.assertAlmostEqual(metre_rig[1], -0.12, places=9)

        # Same motion on a centimetre-authored rig: 12 armature units.
        centimetre_rig = self.hips_location((0.0, crouch, 0.0), 100.0)
        self.assertAlmostEqual(centimetre_rig[1], -12.0, places=6)

    def test_horizontal_movement_has_no_reference_to_subtract(self):
        location = self.hips_location(
            (1.5, self.PERFORMER_HIPS_HEIGHT, -0.4), 1.0)
        self.assertAlmostEqual(location[0], 1.5, places=9)
        self.assertAlmostEqual(location[1], 0.0, places=9)
        self.assertAlmostEqual(location[2], -0.4, places=9)

    def test_the_old_fixed_hundred_would_have_overshot_a_metre_rig(self):
        # What the removed hips_translational_scale=100 did to a metre-authored
        # rig: a 12 cm crouch became a 12 metre drop.
        crouch_delta = -0.12
        self.assertAlmostEqual(crouch_delta * 100.0, -12.0, places=9)
        # Derived instead, the same motion stays 12 cm.
        self.assertAlmostEqual(
            self.hips_location((0.0, self.PERFORMER_HIPS_HEIGHT + crouch_delta, 0.0), 1.0)[1],
            crouch_delta, places=9)

    def test_the_result_is_projected_into_the_hips_rest_axes(self):
        location = self.hips_location(
            (0.0, self.PERFORMER_HIPS_HEIGHT + 0.1, 0.0), 1.0, axes=TILTED_AXES)
        self.assertAlmostEqual(location[1], 0.1 * 0.9998, places=9)
        self.assertAlmostEqual(location[2], 0.1 * -0.0183, places=9)


class UnitFactorTests(unittest.TestCase):
    def test_a_metre_scale_rig_needs_no_scaling(self):
        # samples/blend/MOVINman_V3_Sample: object scale 1.0, bones in metres.
        # The old fixed x100 overshot this rig by a hundred times and pulled its
        # joints apart.
        self.assertAlmostEqual(
            movin.armature_units_per_metre((1.0, 1.0, 1.0), 1.0), 1.0, places=9)

    def test_a_centimetre_scale_rig_needs_the_hundred(self):
        # samples/blend/Ch14_Sample: object scale 0.01, bone data 100x larger.
        # This is the case the hard-coded x100 was silently assuming.
        self.assertAlmostEqual(
            movin.armature_units_per_metre((0.01, 0.01, 0.01), 1.0), 100.0, places=6)

    def test_scene_unit_scale_is_folded_in(self):
        self.assertAlmostEqual(
            movin.armature_units_per_metre((1.0, 1.0, 1.0), 0.01), 100.0, places=6)
        self.assertAlmostEqual(
            movin.armature_units_per_metre((0.1, 0.1, 0.1), 0.1), 100.0, places=6)

    def test_a_degenerate_scale_does_not_divide_by_zero(self):
        self.assertEqual(movin.armature_units_per_metre((0.0, 0.0, 0.0), 1.0), 1.0)
        self.assertEqual(movin.armature_units_per_metre((1.0, 1.0, 1.0), 0.0), 1.0)

    def test_negative_scale_is_treated_by_magnitude(self):
        self.assertAlmostEqual(
            movin.armature_units_per_metre((-1.0, 1.0, 1.0), 1.0), 1.0, places=9)


if __name__ == "__main__":
    unittest.main(verbosity=2)
