"""Measure streamed hips world positions using Blender's actual pose evaluator."""
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bpy
from mathutils import Euler, Matrix, Quaternion, Vector
import _harness


def bone(index, parent, name, p, q, s):
    return {"bone_index": index, "parent_index": parent, "bone_name": name,
            "p": tuple(p), "rq": (1., 0., 0., 0.), "q": tuple(q), "s": tuple(s)}


def main():
    movin = _harness.load_addon()
    scene = bpy.context.scene
    props = scene.movin_props
    arm = _harness.single_armature()
    hips = next(b for b in arm.pose.bones if b.parent is None)
    props.armature_name = arm.name
    props.hips_bone_name = hips.name
    props.pointcloud_enabled = False
    props["hips_y_offset"] = -999.0
    assert "hips_y_offset" not in props.bl_rna.properties
    original = arm.matrix_world.copy()
    identity = (1., 0., 0., 0.)
    unit = (1., 1., 1.)
    max_error = 0.
    frame = 0

    def apply(label, payload, expected_metres):
        nonlocal max_error, frame
        frame += 1
        movin._runtime.ready_frames.append({
            "timestamp": "t", "actor": "Test", "frame_idx": frame, "received_at": time.monotonic(), "bones": payload})
        movin._apply_latest_stream_data(scene.name)
        bpy.context.view_layer.update()
        actual = (arm.matrix_world @ hips.head) * scene.unit_settings.scale_length
        error = (actual - expected_metres).length
        max_error = max(max_error, error)
        assert error < 1e-5, (label, tuple(actual), tuple(expected_metres), error)
        print("PASS: %s, world error %.3e m" % (label, error))

    def source_chain():
        # Two wrappers, sparse indices, and child-first packet order.
        root = bone(10, -1, "SourceWrapper", (.7, -.1, 1.2),
                    Quaternion((0., 1., 0.), .6), (1.4, .7, 1.2))
        parent = bone(30, 10, "SourcePivot", (-.3, .2, .1),
                      Quaternion((1., 0., 0.), -.4), (.8, 1.3, .9))
        child = bone(50, 30, hips.name, (.4, 1.1, 2.), identity, unit)
        # Independent matrix construction, not the add-on's point-folding helper.
        world = (Matrix.LocRotScale(Vector(root["p"]), Quaternion(root["q"]), Vector(root["s"]))
                 @ Matrix.LocRotScale(Vector(parent["p"]), Quaternion(parent["q"]), Vector(parent["s"]))
                 @ Vector(child["p"]))
        return [child, parent, root], Vector((-world.x, -world.z, world.y))

    variants = [
        ("original", original, 1.),
        ("translated", Matrix.Translation((2., -3., .6)) @ original, 1.),
        ("rotated and nonuniform scale", Matrix.Translation((2., -3., .6))
         @ Euler((.3, -.2, .7)).to_matrix().to_4x4()
         @ Matrix.Diagonal((1.3, .8, 1.7, 1.)) @ original, 1.),
        ("negative scale", Matrix.Diagonal((-1., 1., 1., 1.)) @ original, 1.),
        ("centimetre scene", original, .01),
    ]
    for label, matrix, units in variants:
        arm.matrix_world = matrix
        scene.unit_settings.scale_length = units
        bpy.context.view_layer.update()
        for i in range(3):
            p = (.4 + i * .1, 1.1 - i * .12, 2. - i * .2)
            apply(label, [bone(50, -1, hips.name, p, identity, unit)], Vector((-p[0], -p[2], p[1])))
        payload, expected = source_chain()
        apply(label + " with source ancestors", payload, expected)

    parent_object = bpy.data.objects.new("GlobalHipsParent", None)
    scene.collection.objects.link(parent_object)
    parent_object.matrix_world = (Matrix.Translation((1., 2., -3.))
                                  @ Euler((.2, .1, .4)).to_matrix().to_4x4())
    arm.parent = parent_object
    arm.matrix_parent_inverse = Matrix.Identity(4)
    arm.matrix_basis = original
    scene.unit_settings.scale_length = 1.
    bpy.context.view_layer.update()
    payload, expected = source_chain()
    apply("parented Armature", payload, expected)

    # A rolled hips bone under two destination parents, all changing this frame.
    data = bpy.data.armatures.new("GlobalHipsRig")
    arm = bpy.data.objects.new("GlobalHipsRig", data)
    scene.collection.objects.link(arm)
    bpy.ops.object.select_all(action='DESELECT')
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode='EDIT')
    root = data.edit_bones.new("Root")
    root.head, root.tail = (.1, -.2, 0.), (.2, -.1, .4)
    pivot = data.edit_bones.new("Pivot")
    pivot.head, pivot.tail, pivot.parent = (.2, .1, .5), (.3, .2, .8), root
    h = data.edit_bones.new("Hips")
    h.head, h.tail, h.parent = (.1, .2, 1.), (.1, .25, 1.2), pivot
    h.roll = .7
    bpy.ops.object.mode_set(mode='OBJECT')
    hips = arm.pose.bones["Hips"]
    props.armature_name = arm.name
    props.hips_bone_name = hips.name
    arm.matrix_world = (Matrix.Translation((2., -1., .4))
                        @ Euler((.2, -.1, .5)).to_matrix().to_4x4()
                        @ Matrix.Diagonal((1.3, .9, 1.2, 1.)))
    bpy.context.view_layer.update()

    for inherit_scale in ('FULL', 'FIX_SHEAR', 'NONE'):
        hips.bone.inherit_scale = inherit_scale
        for local_location in (True, False):
            hips.bone.use_local_location = local_location
            for i in range(4):
                q = Quaternion((0., 1., 0.), math.pi * i / 6)
                r = bone(10, -1, "Root", (.2 * i, -.1 * i, .3), q, (1.1, .8, 1.2))
                p = bone(30, 10, "Pivot", (.1, .4, -.2), identity, unit)
                h = bone(50, 30, "Hips", (.4, 1.1, 2.), identity, unit)
                world = (Matrix.LocRotScale(Vector(r["p"]), q, Vector(r["s"]))
                         @ Matrix.Translation(p["p"]) @ Vector(h["p"]))
                apply("animated parents %s local=%s" % (inherit_scale, local_location),
                      [h, p, r], Vector((-world.x, -world.z, world.y)))

    bpy.ops.object.mode_set(mode='EDIT')
    data.edit_bones[hips.name].use_connect = True
    bpy.ops.object.mode_set(mode='OBJECT')
    hips = arm.pose.bones["Hips"]
    movin._runtime.ready_frames.append({
        "timestamp": "t", "actor": "Test", "frame_idx": frame + 1, "received_at": time.monotonic(), "bones": [h, p, r]})
    previous = movin._runtime.last_applied
    try:
        movin._apply_latest_stream_data(scene.name)
    except ValueError as error:
        assert "Clear Connected" in str(error)
        assert movin._runtime.last_applied == previous
    else:
        raise AssertionError("Connected hips silently accepted a world position")
    print("PASS: connected hips reports the remedy without claiming an applied frame")
    print("Maximum hips world error: %.3e m" % max_error)
    movin.unregister()


_harness.run_checks(main)
