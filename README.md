# MOVIN Blender Plugin

Blender add-on for receiving and previewing live MOVIN motion and point cloud OSC streams.

This add-on lets you preview MOVIN data directly in Blender by:

- driving a selected armature from `/MOVIN/Frame`
- visualizing `/MOVIN/PointCloud` in the viewport

It is set up for Blender 4.3.2 or newer and includes sample assets for quick testing.

## Highlights

- Live armature retargeting by bone name
- Live point cloud preview in Blender
- Simple N-panel workflow
- Sample `.blend` and `.fbx` files included

## Repository Contents

- `addon/movin_blender_plugin.py`  
  Blender add-on script
- `samples/blend/MOVINman_V3_Sample.blend`
  Sample scene for MOVINman V3
- `samples/blend/Ch14_Sample.blend`  
  Additional sample scene
- `samples/fbx/Ch14_nonPBR.fbx`  
  Sample character FBX

## Installation

1. Open Blender
2. Go to `Edit > Preferences > Add-ons`
3. Click `Install...`
4. Select `addon/movin_blender_plugin.py`
5. Enable `MOVIN Live Receiver`

## Usage

For correct motion transfer, the same `.fbx` character model should be loaded in both MOVIN Studio and Blender.

MOVIN Studio sends position values in meters, so in Blender you will usually want the position-related values to be scaled by `100` to match what you expect visually.

The default `hips_translational_scale` is set to `100.0`.

If the character pose updates correctly but the root movement is barely visible, increase this value.

If the value is too large, the character may move so far that it is no longer visible in the viewport. In that case, reduce the value and check again.

1. Open the `MOVIN Live` tab in the 3D Viewport side panel
2. Select the target armature and click `Use Active Armature`
3. Set the OSC port if needed
4. Enable `Visualize Point Cloud` if you want point cloud preview
5. Click `Start`

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
|     `- Ch14_nonPBR.fbx
|- .gitignore
`- README.md
```

## Recommended Blender Version

- Blender 4.3.2 or newer

## License
Copyright 2025 MOVIN. All Rights Reserved.
