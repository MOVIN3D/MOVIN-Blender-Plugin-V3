# Copyright 2025 MOVIN. All Rights Reserved.

"""Check how streamed transforms land on a real rig, using Blender's own evaluator.

    blender -b samples/blend/MOVINman_V3_Sample.blend --python tests/blender/verify_apply.py

The unit tests cover the arithmetic; this covers the part they cannot. A pure test
can only confirm the code agrees with itself, and that is exactly the trap that
shipped a broken build once: the offset maths was checked against a fixture built
by inverting the add-on's own conversion, passed perfectly, and mangled every
finger on a live stream.

So the load-bearing check here is deliberately frame-independent. Stream an offset
at 1.20x and the joint must sit 1.20x as far from its parent - a fact about the
rig, measured through Blender's pose evaluation, that stays true no matter which
coordinate frame the streamed vector turns out to be in.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bpy                                         # noqa: E402
from mathutils import Vector                       # noqa: E402
import _harness                                    # noqa: E402

#: The performer's standing hips height the hips reference is set to for the test.
PERFORMER_HIPS_HEIGHT = 0.87

#: Ratio used for the magnitude checks.
CALIBRATION_RATIO = 1.20


def main():
    movin = _harness.load_addon()
    scene = bpy.context.scene
    props = scene.movin_props
    arm = _harness.single_armature()

    props.armature_name = arm.name
    props.hips_y_offset = -PERFORMER_HIPS_HEIGHT
    arm.data.pose_position = 'POSE'

    units_per_metre = movin.armature_units_per_metre(
        arm.matrix_world.to_scale(), scene.unit_settings.scale_length)
    rest_frames = movin.collect_bone_rest_frames(arm)

    hips = next(name for name, f in rest_frames.items() if f["offset"] is None)
    props.hips_bone_name = hips
    bones = [name for name, f in rest_frames.items() if f["offset"] is not None]
    connected = [n for n in bones if arm.data.bones[n].use_connect]

    print("armature %r | %d bones | %d connected | %g armature units per streamed metre"
          % (arm.name, len(arm.data.bones), len(connected), units_per_metre))

    def reset_pose():
        for pose_bone in arm.pose.bones:
            pose_bone.location = (0.0, 0.0, 0.0)
            pose_bone.rotation_mode = 'QUATERNION'
            pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
            pose_bone.scale = (1.0, 1.0, 1.0)
        bpy.context.view_layer.update()

    def head(name):
        bpy.context.view_layer.update()
        return arm.pose.bones[name].matrix.to_translation().copy()

    def send(offset_ratio, hips_height=PERFORMER_HIPS_HEIGHT, capture=False):
        """Stream every bone's own rest offset, scaled, plus a hips height."""
        payload = [_bone(hips, (0.0, hips_height, 0.0))]
        for name in bones:
            ox, oy, oz = rest_frames[name]["offset"]
            payload.append(_bone(name, (ox * offset_ratio / units_per_metre,
                                        oy * offset_ratio / units_per_metre,
                                        oz * offset_ratio / units_per_metre)))
        with movin._runtime.lock:
            movin._runtime.ready_frames.clear()
            movin._runtime.ready_frames.append(
                {"timestamp": "t", "actor": "MOVINMan", "frame_idx": 1, "bones": payload})
        if not capture:
            movin._apply_latest_stream_data(scene.name)
            bpy.context.view_layer.update()
            return ""

        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            movin._apply_latest_stream_data(scene.name)
        bpy.context.view_layer.update()
        return buffer.getvalue()

    def _bone(name, blender_offset):
        # The add-on applies unity_to_blender_vec(p) = (-p.x, p.y, p.z); invert it
        # so the value that reaches the maths is the offset we mean.
        return {"bone_name": name, "bone_index": 0, "parent_index": -1,
                "p": (-blender_offset[0], blender_offset[1], blender_offset[2]),
                "rq": (1.0, 0.0, 0.0, 0.0), "q": (1.0, 0.0, 0.0, 0.0),
                "s": (1.0, 1.0, 1.0)}

    reset_pose()
    rest_heads = dict((name, head(name)) for name in arm.pose.bones.keys())
    rest_span = dict(
        (name, (rest_heads[name] - rest_heads[arm.data.bones[name].parent.name]).length)
        for name in bones)

    # -- 1. A Character stream carries the rig's own offsets and must not move it.
    send(1.0)
    drift = max((head(name) - rest_heads[name]).length for name in bones)
    print("\n[1] the rig's own offsets streamed back")
    print("    largest head drift: %.3e armature units over %d bones" % (drift, len(bones)))
    assert drift < 1e-5, "a matching stream displaced the pose"
    print("    OK: identical to the rest pose")

    # -- 2. Frame-independent: 1.20x must put every joint 1.20x from its parent.
    send(CALIBRATION_RATIO)
    worst_error, worst_name, checked = 0.0, None, 0
    for name in bones:
        if arm.data.bones[name].use_connect or rest_span[name] < 1e-6:
            continue
        checked += 1
        span = (head(name) - head(arm.data.bones[name].parent.name)).length
        error = abs(span / rest_span[name] - CALIBRATION_RATIO)
        if error > worst_error:
            worst_error, worst_name = error, name
    print("\n[2] every offset at %.2fx, joint spacing measured by Blender" % CALIBRATION_RATIO)
    print("    worst ratio error: %.3e over %d free bones (%s)" % (worst_error, checked, worst_name))
    assert worst_error < 1e-4, "joint spacing did not scale as streamed"
    print("    OK: this is the check that does not depend on the assumed frame")

    # -- 3. Blender locks a connected bone's location, so those must not scale.
    print("\n[3] connected bones: %d" % len(connected))
    if connected:
        for name in connected:
            if rest_span[name] < 1e-6:
                continue
            span = (head(name) - head(arm.data.bones[name].parent.name)).length
            assert abs(span / rest_span[name] - 1.0) < 1e-6, \
                "%s moved, but Blender should have ignored its location" % name
        print("    OK: none of them moved, as Blender requires")

        # And the note about it reflects the stream, not the rig: a Character
        # stream has nothing to lose, so it must stay quiet.
        movin._runtime.warned_connected_bones = False
        reset_pose()
        matched = send(1.0, capture=True)
        movin._runtime.warned_connected_bones = False
        reset_pose()
        differing = send(CALIBRATION_RATIO, capture=True)
        assert "NOTE:" not in matched, "a matching stream produced a pointless warning"
        assert "NOTE:" in differing, "a dropped offset was not reported"
        print("    OK: the note fires only when an offset is actually dropped")

    # -- 4. Fingers: where a wrong frame shows up first.
    fingers = [n for n in bones if "Hand" in n or "Thumb" in n or "Finger" in n]
    if fingers:
        reset_pose()
        send(1.0)
        drift = max((head(name) - rest_heads[name]).length for name in fingers)
        print("\n[4] %d finger bones, rig's own offsets: largest drift %.3e" % (len(fingers), drift))
        assert drift < 1e-5, "fingers moved on a matching stream"
        print("    OK: fingers hold still")

    # -- 5. Hips: derived scale, performer height as the reference.
    reset_pose()
    rest_hips = head(hips)
    print("\n[5] hips %r, performer standing height %.2f m" % (hips, PERFORMER_HIPS_HEIGHT))

    # Displace it first, so "no drift" cannot pass just because nothing was applied.
    arm.pose.bones[hips].location = (0.5, 0.5, 0.5)
    assert (head(hips) - rest_hips).length > 1e-6
    send(1.0, hips_height=PERFORMER_HIPS_HEIGHT)
    returned = (head(hips) - rest_hips).length
    print("    standing at the reference height -> drift %.3e (from a displaced start)" % returned)
    assert returned < 1e-5, "standing still did not return the hips to its own rest height"

    reset_pose()
    send(1.0, hips_height=PERFORMER_HIPS_HEIGHT - 0.12)
    moved = (head(hips) - rest_hips).length
    expected = 0.12 * units_per_metre
    print("    12 cm crouch -> %.5f armature units, expected %.5f" % (moved, expected))
    assert abs(moved - expected) < 1e-4, "crouch magnitude is wrong"
    print("    OK: scale derived from the rig, and the armature keeps its own hips height")

    reset_pose()


_harness.run_checks(main)
