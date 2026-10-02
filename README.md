# MOVIN Blender Plugin v3.3.0

Receive motion from MOVIN Studio on a Blender Armature and preview streamed point clouds.

## Requirements

- **MOVIN Studio v3.0.0 or later** for motion and point clouds.
- **MOVIN Studio v3.3.0 or later** for connection, bone matching and FPS in Studio.
- Blender add-on minimum: **4.3.2**. Recommended version: **5.2.2 LTS**.
- Included `.blend` samples use **Blender 5.2.2**; use the included FBX models with
  older Blender versions.

For Character streaming, load the same character model in Studio and Blender.

## Installation

1. Download **MOVIN-Blender-Plugin-v3.3.0.zip** from the
   [latest release](https://github.com/MOVIN3D/MOVIN-Blender-Plugin-V3/releases/latest).
2. Open **Edit > Preferences > Add-ons** in Blender.
3. Use **Install from Disk** (`Install` in older Blender versions), select the ZIP,
   and enable **MOVIN Live Receiver**.

For an update, stop streaming, disable the old add-on, install the replacement,
and **fully restart Blender** before enabling it again.

## Quick Start

1. Open a sample scene or select your character's Armature.
2. Open the **MOVIN Live** tab in the 3D Viewport side panel and click **Use Active Armature**.
3. Set **Hips Bone** to the pelvis bone's name and use port **11235**.
4. Enable **Visualize Point Cloud** if needed, then click **Start**.
5. In Studio, select **Blender** and the matching Actor or Character source.
6. Set the destination to the Blender computer's IPv4 address, or `127.0.0.1` when
   both apps run on the same computer. Use the same port and click **Start Streaming**.

Enable **Hand** or **Pointcloud** in Studio to send those streams. Opening another
`.blend` file stops the receiver; click Start again after changing files.

## Sample Scenes

Download **MOVIN-Blender-Samples-v3.3.0.zip** from the same release and extract it.
Open a scene under `samples/blend`; install the add-on separately.

| Scene | Studio source | Hips Bone |
|---|---|---|
| `MOVINman_V3_Sample.blend` | Actor (`MOVINMan`) | `Hips` |
| `Ch14_Sample.blend` | Character using `samples/fbx/Ch14_nonPBR.fbx` | `mixamorig:Hips` |

Both scenes have the Armature and Hips Bone configured and streaming stopped.

## Hips Position and Actor Proportions

The Hips bone follows the world position sent by Studio. Armature placement, parent
transforms and scene units are compensated automatically; no manual Hips offset is needed.
The selected Hips bone must have **Connected** disabled in Edit Mode.

An Actor stream uses the performer's calibrated bone lengths. The MOVINman preview
may deform if those proportions differ from its rest skeleton. **Skeleton Calibration
Offset** describes this difference. For a finished character, stream the matching
Character model from Studio or retarget the Actor motion onto your character.

## Streaming Status

Studio shows the selected Armature, bone mapping and FPS. **Received FPS** measures
incoming motion or point clouds; **Applied FPS** measures motion applied to the Armature.
Armature object names may differ from Studio model names, but the intended skeleton
must match. A bone-mapping result does not compare the meshes themselves.

Allow **UDP 11235** (or your chosen port) into Blender and status replies on
**UDP 39581** into Studio when using separate computers.

## Troubleshooting

| Problem | Check |
|---|---|
| Cannot listen on the port | Stop another receiver using that port, or choose a free port in both apps. |
| No motion | Select the Armature, check Hips Bone, click Start and verify Studio's destination IP and port. |
| Missing or unmatched bones | Load the same character model on both sides and check the streamed bone names. |
| Hips position is incorrect or reception stops | Disable `Connected` on the Hips bone and check constraints on it and its ancestors. |
| A bone's position does not change | Blender locks translation on connected bones; disable `Connected` in Edit Mode if that bone needs streamed translation. |
| Applied FPS drops | Reduce Blender's scene workload or disable point cloud preview when it is not needed. |
| An update appears unchanged | Fully restart Blender after replacing the add-on. |

## License

Copyright 2025 MOVIN. All Rights Reserved.
