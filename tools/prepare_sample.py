"""Write a clean release copy of an open sample, preserving its rig and materials."""
import argparse
import importlib.util
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:])
output = Path(args.output).resolve()
assert output != Path(bpy.data.filepath).resolve(), 'Prepare a separate copy before replacing the sample'
assert not output.exists(), 'Output already exists'
assert len(bpy.data.scenes) == 1
assert not bpy.data.texts, 'Sample must not contain executable scripts'

source = Path(__file__).resolve().parent.parent / 'addon' / 'movin_blender_plugin.py'
spec = importlib.util.spec_from_file_location('movin_prepare', source)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
module.register()
scene = bpy.context.scene
arms = [o for o in scene.objects if o.type == 'ARMATURE']
assert len(arms) == 1
arm = arms[0]
roots = [b for b in arm.data.bones if b.parent is None]
assert len(roots) == 1 and roots[0].name.endswith('Hips')
assert not arm.constraints and not any(b.constraints for b in arm.pose.bones)
if bpy.context.object and bpy.context.object.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')

action = arm.animation_data.action if arm.animation_data else None
arm.animation_data_clear()
arm.data.animation_data_clear()
if action and action.users == int(action.use_fake_user):
    bpy.data.actions.remove(action)
scene.frame_set(1)
arm.data.pose_position = 'POSE'
for bone in arm.pose.bones:
    bone.matrix_basis = Matrix.Identity(4)

props = scene.movin_props
for key in list(props.keys()):
    del props[key]
props.armature_name = arm.name
props.hips_bone_name = roots[0].name
props.port = 11235
props.pointcloud_enabled = True
props.pointcloud_object_name = 'MOVIN_PointCloud'
cloud = scene.objects.get(props.pointcloud_object_name)
if cloud is not None:
    assert cloud.type == 'MESH' and cloud.modifiers.get('MOVIN_PointCloud') is not None
    mesh = cloud.data
    bpy.data.objects.remove(cloud, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)

for image in bpy.data.images:
    if image.source == 'FILE':
        if not image.packed_file:
            image.pack()
        assert image.packed_file, image.name
        image.filepath_raw = '//textures/' + Path(image.filepath).name
        for packed in image.packed_files:
            packed.filepath = image.filepath

bpy.ops.object.select_all(action='DESELECT')
arm.hide_set(False)
arm.select_set(True)
bpy.context.view_layer.objects.active = arm
bpy.context.view_layer.update()
points = [arm.matrix_world @ b.head_local for b in arm.data.bones]
low = Vector(tuple(min(p[i] for p in points) for i in range(3)))
high = Vector(tuple(max(p[i] for p in points) for i in range(3)))
for screen in bpy.data.screens:
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            space = area.spaces.active
            space.region_3d.view_location = (low + high) / 2
            space.region_3d.view_distance = max(high - low) * 2
            space.show_region_ui = True

assert not props.is_running
output.parent.mkdir(parents=True, exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=str(output), compress=True, relative_remap=False)
print('Prepared sample:', output)
module.unregister()
