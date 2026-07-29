# Tests

Nothing here is needed to use the add-on. This is for whoever changes it next.

Run all four before touching the transform maths or the calibration notice.

## Without Blender

The comparison logic is pure Python. `test_skeleton_diagnostics.py` stubs `bpy`
and `mathutils` so the add-on imports outside Blender:

```bash
python tests/test_skeleton_diagnostics.py
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
not move, and the hips must return to its own rest height.

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
