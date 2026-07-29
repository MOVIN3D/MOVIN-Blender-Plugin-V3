# Copyright 2025 MOVIN. All Rights Reserved.

"""Drive the Skeleton Calibration Offset notice end to end against a real rig.

    blender -b samples/blend/MOVINman_V3_Sample.blend --python tests/blender/verify_diagnostics.py

The notice has exactly one failure mode: coming back when nothing the user can see
has changed. It took three separate "it keeps popping up" reports on the Unreal
side before that was pinned down, so most of what follows is repetition testing -
dismiss the notice, keep the pelvis moving, and require silence.

The synthetic stream is shaped like a measured Unreal session against MOVINman:
per-bone ratios that really occurred, and a pelvis whose translation is a world
position landing somewhere different on every frame.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy                                         # noqa: E402
import _harness                                    # noqa: E402

#: Measured against MOVINman in a live Unreal session.
MEASURED_RATIOS = {
    "Neck1": 0.52, "Neck": 1.42, "Head": 0.96,
    "LeftUpLeg": 1.20, "RightUpLeg": 1.20,
    "LeftLeg": 0.93, "RightLeg": 0.93,
    "LeftFoot": 0.93, "RightFoot": 0.93,
    "LeftArm": 1.12, "RightArm": 1.12,
    "LeftForeArm": 0.91, "RightForeArm": 0.91,
    "LeftHand": 0.91, "RightHand": 0.91,
}

#: The pelvis translation is a world position, so it spans roughly this in metres.
HIPS_LOW, HIPS_HIGH = 0.43, 2.38

RESCAN_ROUNDS = 20


def main():
    movin = _harness.load_addon()
    scene = bpy.context.scene
    props = scene.movin_props
    arm = _harness.single_armature()
    props.armature_name = arm.name

    rest_frames = movin.collect_bone_rest_frames(arm)
    hips = next(name for name, f in rest_frames.items() if f["offset"] is None)
    rest_metres = dict(
        (b.name, b.local_offset)
        for b in movin.collect_rest_bones(arm, scene.unit_settings.scale_length))

    # Mixamo rigs prefix their bones; match whatever this rig actually uses.
    prefix = "mixamorig:" if any(n.startswith("mixamorig:") for n in rest_metres) else ""
    ratios = dict((prefix + k, v) for k, v in MEASURED_RATIOS.items())
    streamed = [hips] + [n for n in rest_metres if n in ratios or n.endswith("Spine")]

    print("armature %r | hips %r | %d bones streamed | prefix %r"
          % (arm.name, hips, len(streamed), prefix))

    def frame(index, extra_ratios=None):
        applied = dict(ratios)
        applied.update(extra_ratios or {})
        bones = []
        for name in streamed:
            if name == hips:
                # A world position: somewhere different on every frame.
                length = HIPS_LOW + (HIPS_HIGH - HIPS_LOW) * ((index * 37) % 101) / 100.0
            else:
                length = rest_metres.get(name, 0.1) * applied.get(name, 1.0)
            bones.append({"bone_name": name, "p": (0.0, length, 0.0)})
        return bones

    def observe(count, start=0, extra_ratios=None):
        for index in range(start, start + count):
            with movin._runtime.lock:
                movin._runtime.note_skeleton_locked("MOVINMan", frame(index, extra_ratios))

    def scan():
        movin._runtime.skeleton_last_scan_time = 0.0   # defeat the rate limit
        movin._scan_skeleton_calibration(scene, props)
        with movin._runtime.lock:
            return movin._runtime.skeleton_banner

    def dismiss():
        with movin._runtime.lock:
            movin._runtime.skeleton_banner = None

    # -- 1. Nothing at all until world movement can be told from a bone length.
    observe(movin.SKELETON_MIN_FRAMES_FOR_MOTION_CHECK - 1)
    assert scan() is None, "the notice appeared before the warm-up finished"
    print("\n[1] OK: silent for the first %d frames"
          % movin._runtime.skeleton_frames_observed)

    # -- 2. Raised once, naming real bones and their ratios.
    observe(300, start=movin.SKELETON_MIN_FRAMES_FOR_MOTION_CHECK)
    banner = scan()
    assert banner is not None, "no notice was raised for a deviating stream"
    text = "\n".join(banner["lines"])
    assert "not a plugin error" in text, text
    assert any(("%sx" % movin._display_ratio(r)) in text for r in (0.52, 1.42)), text
    print("\n[2] notice raised:")
    for line in banner["lines"]:
        print("    | %s" % line)

    # -- 3. The whole point: it must not come back on its own.
    repeats = 0
    for round_index in range(RESCAN_ROUNDS):
        dismiss()
        observe(30, start=1000 + round_index * 30)
        if scan() is not None:
            repeats += 1
    print("\n[3] %d rescans after dismissal, pelvis moving throughout" % RESCAN_ROUNDS)
    assert repeats == 0, "the notice re-raised on %d of %d rescans" % (repeats, RESCAN_ROUNDS)
    print("    OK: 0 re-raises")

    # -- 4. A real recalibration must raise it again, with the new figures.
    target = next(name for name in ratios if name in rest_metres)
    dismiss()
    observe(60, start=9000, extra_ratios={target: 0.61})
    banner = scan()
    assert banner is not None, "a recalibration did not re-raise the notice"
    assert "%s 0.61x" % target in "\n".join(banner["lines"]), banner["lines"]
    print("\n[4] OK: recalibration re-raised it, showing %s 0.61x" % target)

    # -- 5. Character streams are already retargeted, so they say nothing.
    movin._runtime.reset()
    for index in range(120):
        with movin._runtime.lock:
            movin._runtime.note_skeleton_locked("Ch14_nonPBR", frame(index))
    assert movin._runtime.skeleton_frames_observed == 0
    assert scan() is None, "a Character stream raised the notice"
    print("\n[5] OK: a Character subject is never observed and never raises it")

    # -- 6. The panel has to survive drawing the thing.
    observe(movin.SKELETON_MIN_FRAMES_FOR_MOTION_CHECK + 5)
    scan()
    rows = _draw_panel(movin, bpy.context)
    assert any("Skeleton Calibration Offset" in r for r in rows), rows
    assert "[op movin.dismiss_skeleton_notice]" in rows, rows
    print("\n[6] OK: panel drew %d rows, with the header and a Dismiss button" % len(rows))


class _Layout:
    """Just enough of Blender's UILayout to run the panel's draw() offline."""

    scale_x = 1.0
    scale_y = 1.0

    def __init__(self, sink):
        self.sink = sink

    def box(self):
        return _Layout(self.sink)

    def column(self, **kwargs):
        return _Layout(self.sink)

    def row(self, **kwargs):
        return _Layout(self.sink)

    def label(self, text="", **kwargs):
        self.sink.append(text)

    def operator(self, idname, **kwargs):
        self.sink.append("[op %s]" % idname)
        return self

    def prop(self, *args, **kwargs):
        pass

    def prop_search(self, *args, **kwargs):
        pass

    def separator(self, **kwargs):
        pass


def _draw_panel(movin, context):
    rows = []

    class _Panel:
        pass

    panel = _Panel()
    panel.layout = _Layout(rows)
    movin.MOVIN_PT_Panel.draw(panel, context)
    return rows


_harness.run_checks(main)
