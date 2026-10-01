"""Headless 60 FPS UDP benchmark; not a viewport FPS guarantee.

Run with either sample .blend. Optionally append -- --baseline path/to/addon.py
to compare an older implementation under the same load. The sender runs in a
separate Python process so packet generation does not compete for Blender's GIL.
"""
import json
import math
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def send(config_path):
    config = json.loads(Path(config_path).read_text())
    packets = [(bytearray.fromhex(data), offset) for data, offset in config["packets"]]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        start = time.perf_counter()
        for frame in range(360):
            delay = start + frame / 60 - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            for data, offset in packets:
                struct.pack_into('>i', data, offset, frame)
                sock.sendto(data, ('127.0.0.1', config["port"]))


def main():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bpy
    import _harness

    if '--baseline' in sys.argv:
        _harness.ADDON_PATH = Path(sys.argv[sys.argv.index('--baseline') + 1])
    ui_work = float(sys.argv[sys.argv.index('--ui-work-ms') + 1]) / 1000 if '--ui-work-ms' in sys.argv else 0.
    movin = _harness.load_addon()
    scene = bpy.context.scene
    props = scene.movin_props
    arm = _harness.single_armature()
    props.armature_name = arm.name
    props.hips_bone_name = next(b.name for b in arm.data.bones if b.parent is None)
    props.pointcloud_enabled = True
    props.pointcloud_object_name = 'MOVIN_BenchmarkCloud'
    indices = {b.name: i for i, b in enumerate(arm.data.bones)}
    bones = [[indices[b.name], indices[b.parent.name] if b.parent else -1, b.name,
              0., 1. if b.parent is None else .01, 0.,
              0., 0., 0., 1., 0., 0., 0., 1., 1., 1., 1.] for b in arm.data.bones]

    def packet(address, args, index_offset):
        def string(value):
            raw = value.encode('utf-8') + b'\0'
            return raw + b'\0' * (-len(raw) % 4)
        tags = ''.join('s' if isinstance(v, str) else 'i' if type(v) is int else 'f' for v in args)
        header = string(address) + string(',' + tags)
        values = [string(v) if t == 's' else struct.pack('>' + t, v) for t, v in zip(tags, args)]
        offset = len(header) + sum(len(v) for v in values[:index_offset])
        return (header + b''.join(values)).hex(), offset

    try:
        for point_count in (0, 1500, 15000):
            packets = []
            count = math.ceil(len(bones) / 7)
            for chunk in range(count):
                part = bones[chunk * 7:(chunk + 1) * 7]
                packets.append(packet('/MOVIN/Frame', ["2026-10-01 12:00:00.000", "Benchmark", 0,
                    count, chunk, len(bones), len(part)] + [v for b in part for v in b], 2))
            points = [(float(i % 100) * .01, float(i // 100) * .01, .5) for i in range(point_count)]
            count = math.ceil(point_count / 76)
            for chunk in range(count):
                part = points[chunk * 76:(chunk + 1) * 76]
                packets.append(packet('/MOVIN/PointCloud', [0, point_count, chunk, count, len(part)]
                                      + [v for p in part for v in p], 0))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(('127.0.0.1', 0))
                props.port = probe.getsockname()[1]
            bpy.ops.movin.start_stream()
            with tempfile.TemporaryDirectory(prefix='movin-benchmark-') as temp:
                config = Path(temp) / 'sender.json'
                config.write_text(json.dumps({'port': props.port, 'packets': packets}))
                python = Path(sys.prefix) / 'bin' / ('python.exe' if sys.platform == 'win32' else 'python3')
                sender = subprocess.Popen([str(python), __file__, '--send', str(config)])
                received, applied, cloud, work = [], [], [], []
                try:
                    start = time.monotonic()
                    while sender.poll() is None:
                        before = time.perf_counter()
                        interval = movin._timer_tick()
                        bpy.context.view_layer.update()
                        elapsed = time.monotonic() - start
                        if 1.5 < elapsed < 5.5:
                            now = time.monotonic()
                            with movin._runtime.lock:
                                received.append(movin.frame_rate(movin._runtime.motion.received_times, now))
                                applied.append(movin.frame_rate(movin._runtime.applied_times, now))
                                cloud.append(movin.frame_rate(movin._runtime.cloud.received_times, now))
                            work.append((time.perf_counter() - before) * 1000)
                        assert interval is not None, movin._runtime.socket_error
                        # Simulated UI work, not an actual viewport measurement.
                        time.sleep(interval + ui_work)
                    assert sender.returncode == 0
                finally:
                    if sender.poll() is None:
                        sender.terminate()
                        sender.wait()
            assert applied, 'No samples collected'
            print('BENCH ' + json.dumps({
                'points': point_count, 'simulated_ui_ms': ui_work * 1000, 'received_fps': statistics.mean(received),
                'applied_fps': statistics.mean(applied), 'pointcloud_fps': statistics.mean(cloud),
                'main_work_ms': statistics.median(work), 'main_work_p95_ms': sorted(work)[int(len(work) * .95)],
            }), flush=True)
            bpy.ops.movin.stop_stream()
    finally:
        movin.unregister()


if __name__ == '__main__':
    if '--send' in sys.argv:
        send(sys.argv[sys.argv.index('--send') + 1])
    else:
        main()
