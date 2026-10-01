"""Check release sample defaults without changing or saving the scene."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bpy
import _harness


def main():
    module = _harness.load_addon()
    scene = bpy.context.scene
    props = scene.movin_props
    arm = _harness.single_armature()
    expected = {'MOVINman_V3_Sample': ('G5_MVMAN', 'Hips', 54),
                'Ch14_Sample': ('Armature', 'mixamorig:Hips', 57)}
    name, hips, count = expected[Path(bpy.data.filepath).stem]
    assert (arm.name, props.hips_bone_name, len(arm.data.bones)) == (name, hips, count)
    assert props.armature_name == arm.name and props.port == 11235
    assert props.pointcloud_enabled and props.pointcloud_object_name == 'MOVIN_PointCloud'
    assert not props.is_running and not arm.animation_data and not arm.data.animation_data
    assert not {'hips_y_offset', 'hips_translational_scale', 'is_running'}.intersection(props.keys())
    assert not arm.data.bones[hips].use_connect
    assert arm.data.pose_position == 'POSE'
    assert scene.frame_current == 1
    for bone in arm.pose.bones:
        assert all(abs(bone.matrix_basis[r][c] - (1 if r == c else 0)) < 1e-5
                   for r in range(4) for c in range(4)), bone.name
    assert scene.objects.get('MOVIN_PointCloud') is None, 'Live capture left in sample'
    assert not bpy.data.texts and not bpy.data.libraries
    assert all(i.packed_file for i in bpy.data.images if i.source == 'FILE')
    assert all(i.filepath.replace('\\', '/').startswith('//textures/') and
               all(p.filepath.replace('\\', '/').startswith('//textures/') for p in i.packed_files)
               for i in bpy.data.images if i.source == 'FILE')
    assert bpy.context.view_layer.objects.active == arm and arm.select_get()
    print('PASS: clean pose, no animation or legacy settings, valid receiver defaults, packed textures')
    module.unregister()


_harness.run_checks(main)
