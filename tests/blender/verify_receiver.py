"""Real sockets, Blender lifecycle, status replies, and missing root transforms."""
import sys
import socket
import time
import struct
import math
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bpy
from mathutils import Vector
import _harness


def packet(address, args):
    def string(value):
        data = value.encode('utf-8') + b'\0'
        return data + b'\0' * (-len(data) % 4)
    tags = ''.join('s' if isinstance(v, str) else 'i' if type(v) is int else 'f' for v in args)
    return string(address) + string(',' + tags) + b''.join(
        string(v) if t == 's' else struct.pack('>' + t, v) for t, v in zip(tags, args))


def main():
    m = _harness.load_addon()
    scene = bpy.context.scene
    props = scene.movin_props
    arm = _harness.single_armature()
    props.armature_name = arm.name
    props.pointcloud_enabled = False
    hips = next(b.name for b in arm.data.bones if b.parent is None)
    props.hips_bone_name = hips
    assert not props.is_running, 'Saved .blend state must not claim a running receiver'
    for pb in arm.pose.bones:
        pb.location = (0, 0, 0)
        pb.rotation_mode = 'QUATERNION'
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.scale = (1, 1, 1)

    def free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(('127.0.0.1', 0))
            return probe.getsockname()[1]

    def bone(index, parent, name, pos):
        return [index, parent, name, *pos, 0., 0., 0., 1., 0., 0., 0., 1., 1., 1., 1.]

    def receive(index, x):
        bones = bone(0, -1, 'FBX Object Root', (x, 0., 0.)) + bone(1, 0, hips, (0., 0., 0.))
        data = packet('/MOVIN/Frame', ['2026-10-01 12:00:00.000', 'Test', index, 1, 0, 2, 2] + bones)
        source.sendto(data, ('127.0.0.1', props.port))
        deadline = time.monotonic() + 2
        while m._runtime.motion.last_frame != index and time.monotonic() < deadline:
            time.sleep(.005)
        assert m._runtime.motion.last_frame == index
        assert m._timer_tick() <= 1 / 60
        bpy.context.view_layer.update()
        return arm.matrix_world @ arm.pose.bones[hips].head

    try:
        for _ in range(3):
            props.port = free_port()
            assert bpy.ops.movin.start_stream() == {'FINISHED'}
            worker = m._runtime.thread
            assert props.is_running and bpy.app.timers.is_registered(m._timer_tick)
            assert bpy.ops.movin.stop_stream() == {'FINISHED'}
            assert not worker.is_alive() and not props.is_running
            assert not bpy.app.timers.is_registered(m._timer_tick)
            assert m._timer_tick() is None
        print('PASS: three Start/Stop cycles leave no thread or timer')

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as busy:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                busy.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            busy.bind(('0.0.0.0', 0))
            props.port = busy.getsockname()[1]
            try:
                bpy.ops.movin.start_stream()
            except RuntimeError:
                pass  # Blender promotes operator error reports to exceptions.
            assert not props.is_running and m._runtime.socket_error
            assert m._runtime.sock is None and not bpy.app.timers.is_registered(m._timer_tick)
        print('PASS: port conflict reports failure and releases all resources')

        props.port = free_port()
        bpy.ops.movin.start_stream()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as source, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reply:
            reply.bind(('127.0.0.1', 0))
            reply.settimeout(2)
            p0 = receive(1, 0.)
            p1 = receive(2, 1.)
            assert abs((p1-p0).length - 1.) < 1e-5, (p1-p0).length
            assert m._runtime.matched == 1 and m._runtime.missing == 0
            print('PASS: missing FBX root moves the rig exactly one metre')
            source.sendto(packet('/MOVIN/Blender/Status/Request', ['a'*32, reply.getsockname()[1]]), ('127.0.0.1', props.port))
            deadline = time.monotonic() + 2
            while m._runtime.status_request is None and time.monotonic() < deadline:
                time.sleep(.005)
            m._timer_tick()
            address, args = m._OscReader(reply.recv(4096)).read_message()
            assert address == '/MOVIN/Blender/Status' and args[0] == 'a'*32
            assert args[2] == arm.name and args[4:6] == [1, 0]
            assert args[6] > 0 and args[7] > 0 and args[11] == 1 and args[12] == 0
            print('PASS: OSC status reply describes the selected rig and actual application')

            root = bone(0, -1, 'FBX Object Root', (0., 0., 0.))
            root[12:14] = [math.sqrt(.5), math.sqrt(.5)]
            root[14:17] = [2., 2., 2.]
            data = packet('/MOVIN/Frame', ['2026-10-01 12:00:00.000', 'Test', 3, 1, 0, 2, 2]
                          + root + bone(1, 0, hips, (1., 0., 0.)))
            with m._runtime.lock:
                m._receive_packet(data, m._runtime.motion.sender, time.monotonic())
            m._timer_tick()
            bpy.context.view_layer.update()
            head = arm.matrix_world @ arm.pose.bones[hips].head
            assert abs((head-p0).length - 2.) < 1e-5
            assert abs(arm.pose.bones[hips].rotation_quaternion.angle - math.pi / 2) < 1e-5
            assert tuple(arm.pose.bones[hips].scale) == (2., 2., 2.)
            print('PASS: missing parent rotation and scale reach the mapped child')

            # A slow consumer must resume near live time, then drain in order.
            def queued(index, received_at):
                data = packet('/MOVIN/Frame', ['2026-10-01 12:00:00.000', 'Test', index, 1, 0, 1, 1]
                              + bone(0, -1, hips, (index * .001, 1., 0.)))
                with m._runtime.lock:
                    m._receive_packet(data, m._runtime.motion.sender, received_at)
            for index in range(4, 104):
                queued(index, time.monotonic())
            assert [f['frame_idx'] for f in m._runtime.ready_frames] == [102, 103]
            for index in (102, 103):
                m._timer_tick()
                bpy.context.view_layer.update()
                assert m._runtime.last_applied == index
                world = arm.matrix_world @ arm.pose.bones[hips].head
                assert (world - Vector((-index * .001, 0., 1.))).length < 1e-5
            applied_count = len(m._runtime.applied_times)
            m._timer_tick()
            assert len(m._runtime.applied_times) == applied_count
            queued(104, time.monotonic() - .1)
            m._timer_tick()
            assert m._runtime.last_applied == 103 and not m._runtime.ready_frames
            queued(105, time.monotonic())
            m._timer_tick()
            assert m._runtime.last_applied == 105
            queued(106, time.monotonic() - .1)
            queued(107, time.monotonic())
            m._timer_tick()
            assert m._runtime.last_applied == 107 and not m._runtime.ready_frames
            print('PASS: two-frame buffer drains in order, bounds lag, and discards stale poses')

        props.pointcloud_enabled = True
        props.pointcloud_object_name = 'ReceiverTestCloud'
        scene.unit_settings.scale_length = .01
        for points in [[(1., 2., 3.)], [(2., 3., 4.)], [(2., 3., 4.), (-1., -.2, -.3)], []]:
            m._runtime.ready_pointclouds.append({'points': points})
            m._timer_tick()
            mesh = bpy.data.objects['ReceiverTestCloud'].data
            assert len(mesh.vertices) == len(points)
            for vertex, (x, y, z) in zip(mesh.vertices, points):
                assert (vertex.co - Vector((-x * 100, -z * 100, y * 100))).length < 1e-4
            modifier = bpy.data.objects['ReceiverTestCloud'].modifiers['MOVIN_PointCloud']
            radius = modifier.properties.inputs.Socket_2.value if bpy.app.version >= (5, 2, 0) else modifier['Socket_2']
            assert abs(radius - 2.) < 1e-6
            assert len(mesh.materials) == 1
        untouched = bpy.data.objects.new('ExistingUserMesh', bpy.data.meshes.new('ExistingUserMesh'))
        scene.collection.objects.link(untouched)
        try:
            m._update_pointcloud_object(scene, untouched.name, [(1., 1., 1.)], .02, (1., 1., 1., 1.))
            raise AssertionError('Must not overwrite an unrelated mesh')
        except ValueError:
            assert len(untouched.data.vertices) == 0
        print('PASS: point cloud vertex reuse, empty frames, scene units, and mesh ownership')

        worker = m._runtime.thread
        m._before_file_load(None)
        assert not worker.is_alive() and not props.is_running
        assert not bpy.app.timers.is_registered(m._timer_tick)
        bpy.ops.movin.start_stream()
        worker = m._runtime.thread
        m.unregister()
        assert not worker.is_alive() and not m._runtime.running
        assert not bpy.app.timers.is_registered(m._timer_tick)
        print('PASS: file load and add-on disable stop receiving')
    finally:
        if hasattr(bpy.types.Scene, 'movin_props'):
            m.unregister()


_harness.run_checks(main)
