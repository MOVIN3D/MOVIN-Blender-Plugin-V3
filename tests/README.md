# Tests

Nothing here is needed to use the add-on. This is for whoever changes it next.

Run these checks before touching the transform maths or the calibration notice.

## Without Blender

The comparison logic is pure Python. `test_skeleton_diagnostics.py` stubs `bpy`
and `mathutils` so the add-on imports outside Blender:

```bash
python tests/test_skeleton_diagnostics.py
python tests/test_receiver.py
```

`mutation_check.py` puts each bug the suite guards against back in and confirms
the tests turn red. A test that cannot fail is not protecting anything:

```bash
python tests/mutation_check.py
```

Three mutations survived the first time it ran, which is how two real holes were
found - the pelvis exclusion was never checked as being *derived* from
observation, and the ranking and signature fixes were each masking the other.

## With Blender

Run each against both sample scenes. They exit non-zero on failure, which
`blender -b` does not do on its own - it swallows a script's traceback and still
reports success, so everything here goes through `_harness.run_checks()`.

```bash
blender -b samples/blend/MOVINman_V3_Sample.blend --python tests/blender/verify_apply.py
```

```bash
blender -b samples/blend/Ch14_Sample.blend --python tests/blender/verify_apply.py
```

`verify_apply.py` checks streamed transforms against Blender's own pose
evaluator: a matching stream must leave the pose untouched, connected bones must
not move, and the hips must reach the streamed world position.

```bash
blender -b samples/blend/MOVINman_V3_Sample.blend --python tests/blender/verify_global_hips.py
blender -b samples/blend/Ch14_Sample.blend --python tests/blender/verify_global_hips.py
```

`verify_global_hips.py` exercises the production application loop with translated,
rotated, scaled and parented Armatures, scene unit changes, and source parent
transforms. It also checks a parented hips bone while its parent changes in the
same frame, legacy saved offsets, and rejection of connected hips. Expected
positions are measured in world metres through Blender's pose evaluator.

```bash
blender -b samples/blend/MOVINman_V3_Sample.blend --python tests/blender/verify_diagnostics.py
```

```bash
blender -b samples/blend/Ch14_Sample.blend --python tests/blender/verify_diagnostics.py
```

`verify_diagnostics.py` drives the calibration notice end to end. Most of it is
repetition testing: dismiss the notice, keep the pelvis moving, require silence.
Coming back on its own is the only failure mode that feature has, and it took
three separate "it keeps popping up" reports on the Unreal side to pin down.

## Why the Blender checks exist

A pure test can only confirm the code agrees with itself, and that is exactly the
trap that shipped a broken build once. The bone offset maths was verified against
a fixture built by inverting the add-on's own conversion, passed perfectly, and
mangled every finger on a live stream - because the assumption under test was
baked into the fixture.

So the load-bearing assertion in `verify_apply.py` is deliberately
frame-independent: stream an offset at 1.20x and the joint must sit 1.20x as far
from its parent. That is a fact about the rig, measured through Blender's
evaluation, and it stays true no matter which coordinate frame the streamed
vector turns out to be in.

Both sample rigs are worth running, and not interchangeable. `MOVINman_V3_Sample`
is an Actor rig at metre scale with no connected bones; `Ch14_Sample` is a
Character rig at 0.01 object scale whose bones average 116 degrees off the
armature axes and which keeps 5 connected bones. Bugs that hide on one show up
on the other.

`verify_sample.py` checks the saved release defaults: matching Armature and Hips
selection, a clean pose, no active animation or captured cloud, no legacy Hips
settings, packed textures, and a stopped receiver. Run it against both scenes
after preparing them with `tools/prepare_sample.py`.

Run `tests/blender/verify_receiver.py` against both samples as well. It opens real
UDP sockets and checks Start/Stop cleanup, occupied ports, saved running state,
file-load and add-on-disable cleanup, missing FBX root translation, and status
replies. It also checks ordered motion application, the two-frame queue limit,
and discarding poses older than 50 ms. All test ports are temporary; the sample
files are not saved.

`test_receiver.py` covers packet validation, interleaved and reordered frames,
sender isolation, index restart, bounded partial buffers, and removal of the
network file-writing commands. These tests do not replace live visual checks of
a Studio stream on the supported Blender versions.

## Performance comparison

```bash
blender --factory-startup --disable-autoexec -b samples/blend/Ch14_Sample.blend --python-exit-code 1 --python tests/blender/benchmark_stream.py
```

The benchmark sends full motion plus 0, 1,500, and 15,000 points at 60 FPS over
real UDP from a separate Python process. It reports received/applied FPS and main
thread work time after warmup. Append `-- --baseline path/to/older_addon.py` to
measure an older implementation with the same fixture. Run comparisons
sequentially to avoid CPU contention. Ports are temporary and scenes are not
saved. This measures headless reception and pose/geometry evaluation; viewport
rendering and the user's scene can add further costs.

Append `--ui-work-ms 8` after `--` to simulate an additional 8 ms of UI work
between callbacks. This exposes extra timer delays without claiming to measure
the real viewport. The output labels this simulated cost explicitly.
