# MOVIN Blender Plugin

Blender add-on for receiving and previewing live MOVIN motion and point cloud OSC streams.

This add-on lets you preview MOVIN data directly in Blender by:

- driving a selected armature from `/MOVIN/Frame`
- visualizing `/MOVIN/PointCloud` in the viewport

It is set up for Blender 4.3.2 or newer and includes sample assets for quick testing.

## Highlights

- Live armature retargeting by bone name
- Live point cloud preview in Blender
- Bone offsets and hips translation scaled from the rig, with nothing to dial in
- Skeleton Calibration Offset notice, so an Actor stream's proportions are
  explained rather than mistaken for a bug
- Simple N-panel workflow
- Sample `.blend` and `.fbx` files included

## Repository Contents

- `addon/movin_blender_plugin.py`  
  Blender add-on script
- `samples/blend/MOVINman_V3_Sample.blend`  
  Sample scene for MOVINman V3, the Actor rig
- `samples/blend/Ch14_Sample.blend`  
  Sample scene for Ch14, a Character rig
- `samples/fbx/MOVINman_V3_Puppet.fbx`  
  MOVINman V3 source FBX
- `samples/fbx/Ch14_nonPBR.fbx`  
  Sample character FBX
- `tests/test_skeleton_diagnostics.py`  
  Unit tests for the comparison logic; runs without Blender
- `tests/mutation_check.py`  
  Puts each known trap back and confirms the tests catch it
- `tests/blender/`  
  Checks that need a real rig, run through `blender -b`

## Installation

1. Open Blender
2. Go to `Edit > Preferences > Add-ons`
3. Click `Install...`
4. Select `addon/movin_blender_plugin.py`
5. Enable `MOVIN Live Receiver`

## Usage

For correct motion transfer, the same `.fbx` character model should be loaded in both MOVIN Studio and Blender.

MOVIN Studio sends position values in meters, while bone data lives in the
armature's own units. The two are related by the armature object's scale and
`Scene > Units > Unit Scale`, so the factor differs per rig:

| Rig | Armature object scale | Armature units per streamed metre |
| --- | --- | --- |
| `MOVINman_V3_Sample` (authored in metres) | 1.0 | 1 |
| `Ch14_Sample` (authored in centimetres) | 0.01 | 100 |

The add-on derives this factor from the rig, for the hips as well as for every
other bone, so there is no scale to dial in.

The one value still set by hand is `Performer Hips Height (m)`, defaulting to
`-0.87`: the performer's standing hips height, negated. Streamed hips height is
measured against it, so only the movement away from standing is transferred and
the armature keeps its own hips height. Set it to the performer's actual standing
hips height if the character floats or sinks.

### Bone offsets and connected bones

Each bone's streamed offset is applied against the armature's **rest pose** - the
`.fbx`'s own value - so the result does not depend on which frame arrived first
and needs no baseline to reset. A Character stream is already retargeted onto the
same `.fbx`, so its offsets equal the rest offsets and nothing is displaced;
an Actor stream carries the performer's proportions and does displace the joints.

Both sides are taken in the **parent bone's rest frame**, which is the frame
Blender's pose evaluation works in:

```text
pose = parent_pose @ (parent_rest^-1 @ rest) @ basis
```

Using the bone's own axes and an armature-space offset instead is a different
quantity. It looks fine on bones that sit near the armature axes and is badly
wrong elsewhere - MOVINman's bones average 12 degrees off with thumbs at 60, and
Ch14's average 116 degrees, which is why fingers were the first thing to break.

Blender locks the location channel of a bone with `Connected` set, so an offset
written to one goes nowhere. `MOVINman_V3_Sample` had 6, including `Neck` and
`Neck1` - the two largest deviations in practice - so `Connected` has been cleared
on that rig; clearing it moves nothing, it only stops each head being pinned to
its parent's tail. `Ch14_Sample` keeps its 5, which come from the `.fbx` itself
and would return on any re-import. The add-on notes blocked bones in the console
once per session, and only when the stream actually carries an offset being
dropped - a Character stream stays quiet.

1. Open the `MOVIN Live` tab in the 3D Viewport side panel
2. Select the target armature and click `Use Active Armature`
3. Set the OSC port if needed
4. Enable `Visualize Point Cloud` if you want point cloud preview
5. Click `Start`

## Skeleton Calibration Offset

MOVIN Studio calibrates the streamed skeleton to the performer's body, so an
Actor stream carries that performer's bone lengths rather than the ones built
into your armature. When the two disagree, the add-on says so once in the
`MOVIN Live` panel, naming the bones and the actual ratios:

```text
Skeleton Calibration Offset
Subject 'MOVINMan' is streaming bone lengths calibrated to the
performer's body. Armature 'G5_MVMAN' was built to different
proportions, so the two skeletons differ at these bones
(streamed / rest length, largest first):
    Neck1 0.52x, Neck 1.42x
    LeftUpLeg 1.20x, RightUpLeg 1.20x
    ...
```

The mesh visibly changes shape at those joints. This is guidance, not an error:
it is what motion data arriving without loss looks like, and there is nothing to
fix - applying the offset and leaving the mesh undeformed are the same handle, so
a skinned mesh cannot have both. The notice stays up until you click `Dismiss`,
and only returns if MOVIN Studio recalibrates.

For a finished character rather than a preview, stream a Character from MOVIN
Studio - already retargeted onto the same `.fbx`, so no offset arises - or
retarget the take onto your own rig.

Notes:

- Character streams are never reported on, only Actor streams.
- Nothing is shown for the first 30 frames, while the add-on works out which
  bones carry world movement rather than a bone length.
- The comparison is normalised to metres through the armature object's scale and
  `Scene > Units > Unit Scale`. If those cannot be reconciled the report is
  suppressed and the reason logged to the console rather than reporting every
  bone at 100x.
- `Print Status` dumps the diagnostic state, including the computed signature and
  the bones excluded as world movement.

## Tests

The comparison logic is pure Python and runs without Blender:

```bash
python tests/test_skeleton_diagnostics.py
```

Every trap the suite guards against can be put back, to confirm the tests still
catch it. A test that cannot fail is not protecting anything:

```bash
python tests/mutation_check.py
```

The rest needs a real rig, because a pure test can only confirm the code agrees
with itself. Run each against both sample scenes; they exit non-zero on failure:

```bash
blender -b samples/blend/MOVINman_V3_Sample.blend --python tests/blender/verify_apply.py
```

```bash
blender -b samples/blend/MOVINman_V3_Sample.blend --python tests/blender/verify_diagnostics.py
```

`verify_apply.py` checks streamed transforms against Blender's own pose evaluator,
including one deliberately frame-independent assertion: stream an offset at 1.20x
and the joint must sit 1.20x as far from its parent, whatever coordinate frame the
streamed vector turns out to be in. `verify_diagnostics.py` drives the notice end
to end, mostly to prove it does not come back on its own.

## OSC Formats

### `/MOVIN/Frame`

Header:

`[timestamp, actorName, frameIdx, numChunks, chunkIdx, totalBoneCount, chunkBoneCount]`

Per-bone payload:

`[boneIndex, parentIndex, boneName, px, py, pz, rqx, rqy, rqz, rqw, qx, qy, qz, qw, sx, sy, sz]`

### `/MOVIN/PointCloud`

Header:

`[frameIdx, totalPoints, chunkIdx, numChunks, chunkPointCount]`

Per-point payload:

`[x, y, z]`

## Project Structure

```text
MOVIN_Blender/
|- addon/
|  `- movin_blender_plugin.py
|- samples/
|  |- blend/
|  |  |- MOVINman_V3_Sample.blend
|  |  `- Ch14_Sample.blend
|  `- fbx/
|     |- MOVINman_V3_Puppet.fbx
|     `- Ch14_nonPBR.fbx
|- tests/
|  |- test_skeleton_diagnostics.py
|  |- mutation_check.py
|  `- blender/
|     |- _harness.py
|     |- verify_apply.py
|     `- verify_diagnostics.py
|- .gitignore
`- README.md
```

## Recommended Blender Version

- Blender 4.3.2 or newer

## License
Copyright 2025 MOVIN. All Rights Reserved.
