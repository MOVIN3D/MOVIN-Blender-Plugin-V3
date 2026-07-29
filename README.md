# MOVIN Blender Plugin

Blender add-on for receiving and previewing live MOVIN motion and point cloud OSC
streams. Needs Blender 4.3.2 or newer; sample scenes are included.

## Highlights

- Live armature retargeting by bone name, from `/MOVIN/Frame`
- Live point cloud preview, from `/MOVIN/PointCloud`
- Nothing to scale by hand - bone and hips translation are read from the rig
- A panel notice explaining an Actor stream's proportions, so they are not
  mistaken for a bug
- Simple N-panel workflow

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
- `tests/`  
  Tests for the add-on itself, not needed to use it - see [tests/README.md](tests/README.md)

## Installation

1. Open Blender
2. Go to `Edit > Preferences > Add-ons`
3. Click `Install...`
4. Select `addon/movin_blender_plugin.py`
5. Enable `MOVIN Live Receiver`

## Usage

For correct motion transfer, the same `.fbx` character model should be loaded in
both MOVIN Studio and Blender.

1. Open the `MOVIN Live` tab in the 3D Viewport side panel
2. Select the target armature and click `Use Active Armature`
3. Set the OSC port if needed
4. Enable `Visualize Point Cloud` if you want point cloud preview
5. Click `Start`

### Hips Height Offset

The one setting you normally need to touch, defaulting to `-0.87` metres. The
streamed hips height is the performer's, measured from wherever MOVIN Studio's
origin sits, so it does not line up with your armature's hips on its own. This
offset is added to it before it is applied.

Tune it until the character stands at the right height - raise it if the character
sinks into the floor, lower it if it floats. From there only the movement away
from that pose is transferred, so the armature keeps its own hips height.

Everything else scales itself. Streamed metres are converted using a factor read
from the armature object's scale and `Scene > Units > Unit Scale`, which is why
the old `Hips Translational Scale` and `Bone Local Position Scale` settings are
gone as of 1.1.0.

### If a bone does not follow the stream

Blender locks the location channel of a bone that has `Connected` set, so the
streamed bone offset cannot reach it. The add-on lists any affected bones in the
console once per session; clear `Connected` on them in Edit Mode if you need those
offsets applied. Clearing it does not move anything.

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

- Only Actor streams are reported on. A Character stream matches your armature
  by definition, so there is nothing to say about it.
- Nothing appears for the first few seconds of a stream, while the add-on works
  out which bones carry world movement rather than a bone length.
- `Print Status` prints the full diagnostic state to the console.

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

## License

Copyright 2025 MOVIN. All Rights Reserved.
