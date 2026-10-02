# MOVIN Blender Plugin

**MOVIN Blender Plugin v3.3.0 supports motion and point cloud streaming from
MOVIN Studio v3.0.0 and later.**

Studio connection status, bone-mapping results, and FPS feedback require
**MOVIN Studio v3.3.0 or later**.

**v3.3.0 was refreshed on 2026-10-02** to fix reception after a sender clock rollback.
Use the current release checksums to identify this build and fully restart Blender after updating.

Blender add-on for receiving and previewing live MOVIN motion and point cloud OSC
streams.

The add-on retains its Blender 4.3.2 minimum; this release is tested on Blender
5.2.2 LTS. The included `.blend` samples are saved and tested in 5.2.2. Use that
version for the prepared scenes; on older versions, import the included FBX
models instead. Older Blender versions have not been revalidated for this release.

## Highlights

- Live armature retargeting by bone name, from `/MOVIN/Frame`
- Live point cloud preview, from `/MOVIN/PointCloud`
- Global hips position follows Studio, with automatic coordinate and unit conversion
- A panel notice explaining an Actor stream's proportions, so they are not
  mistaken for a bug
- Simple N-panel workflow
- Studio feedback for connection, selected Armature, bone mapping, and received/applied FPS

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

1. Download `MOVIN-Blender-Plugin-v3.3.0.zip` from the
   [release downloads](https://github.com/MOVIN3D/MOVIN-Blender-Plugin-V3/releases).
2. In Blender, open `Edit > Preferences > Add-ons`.
3. Open the menu at the top right and choose `Install from Disk...`
   (`Install...` in older Blender versions).
4. Select the plugin ZIP and enable `MOVIN Live Receiver`.

To install from this repository, select `addon/movin_blender_plugin.py` instead.
For an update, stop streaming, disable the old add-on, install the replacement,
and fully restart Blender before enabling it again. Toggling the add-on alone
can leave the previous Python module in memory.

`MOVIN-Blender-Samples-v3.3.0.zip` is a separate download: extract it to a folder
and open a scene under `samples/blend`. Do not install the samples ZIP as an
add-on. Both scenes have their Armature and Hips Bone selected, port `11235`,
point cloud preview enabled, and streaming stopped. They open in their rest
pose without an active animation or a captured point cloud. Textures are packed
inside the Ch14 scene; the add-on itself is installed separately.

| Blender scene | Studio source | Hips Bone |
| --- | --- | --- |
| `MOVINman_V3_Sample.blend` | Actor (`MOVINMan`) | `Hips` |
| `Ch14_Sample.blend` | Character loaded from `samples/fbx/Ch14_nonPBR.fbx` | `mixamorig:Hips` |

The Ch14 rig keeps its five original connected bones. Stream the matching
Character model to it; connected bone translations are locked by Blender.

## Usage

For Character streaming, load the same `.fbx` model in MOVIN Studio and Blender.

1. Open the `MOVIN Live` tab in the 3D Viewport side panel
2. Select the target armature and click `Use Active Armature`
3. Set `Hips Bone` to the streamed pelvis bone's name and set the OSC port if needed
4. Enable `Visualize Point Cloud` if you want point cloud preview
5. Click `Start`

In MOVIN Studio, select **Blender**, enter this computer's IPv4 address and the
same port, then start streaming. The default receiving port is `11235`.
Studio receives status replies on UDP `39581`; allow both ports through the
firewall when using separate computers.

The Studio status window shows the selected Armature and whether the streamed
bones can be mapped to it. Armature object names may differ from Studio model
names. Extra parent transform nodes in an FBX are folded into their mapped
descendants, so an absent FBX wrapper is not reported as a missing bone.
This checks bone mapping, not mesh identity or whether constraints override the
result. Use the same model on both sides.

**Received FPS** counts complete incoming frames; **Applied FPS** counts motion
frames written to the selected Armature. The application timer checks for new
frames at 120 Hz to accommodate a 60 FPS stream; actual performance depends on
Blender's workload. Callback work is included in that interval instead of adding
another full delay after each application. Turning off Hand in Studio omits
finger subtrees and keeps the wrists. Omitted bones retain their current Blender pose.

Point cloud reception decodes numeric OSC payloads in bulk. Preview coordinates
are converted in arrays and copied directly into the mesh; unchanged Geometry
Nodes and material settings are reused. Point clouds use the latest complete
frame, and the existing 15,000-point preview limit is unchanged.

Motion keeps up to two complete frames and applies them in order to absorb
arrival jitter. A new frame replaces the oldest when the queue is full, so lag
cannot accumulate. Queued motion older than 50 ms is discarded before applying;
source, character, or frame-index restarts clear the relevant queued frames.
At 60 FPS this can add roughly one frame of latency compared with always taking
the newest pose. Point cloud display is not delayed to match this small buffer.

Stop releases the receiving port and timer. Opening another `.blend` or disabling
the add-on also stops the receiver. Start again after changing files. A failed
port bind leaves the receiver stopped and displays the error.

### Global Hips Position

Set `Hips Bone` to the pelvis bone present in both the stream and the selected
Armature (for example, `Hips` or `mixamorig:Hips`). Its world position follows the
position sent by Studio, measured from the streaming origin. The add-on combines
the streamed parent transforms, converts Unity metres to Blender coordinates
`(-x, -z, y)`, and accounts for `Scene > Units > Unit Scale`.

The Armature's world transform and the bone's rest and parent transforms are
compensated when positioning the hips. Moving, rotating, or scaling the Armature
does not add an offset to the streamed hips position. No reference pose or manual
height adjustment is needed; the former `Hips Height Offset` setting is removed
and values saved by older versions are ignored.

The hips must not be `Connected` to its parent; otherwise the receiver stops with
an explanation because Blender locks that bone's translation. Constraints on
the hips or its ancestors can also override the result. Use an unconstrained
streaming rig for direct motion application.

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

### Reception and status

Studio keeps Blender motion and point-cloud datagrams within 1,200 bytes,
including OSC metadata and UTF-8 names. Chunk sizes can vary; receivers must use
the counts in the header. Bone indices retain their original hierarchy indices
when Hand is off, so they can have gaps.

The receiver discards incomplete, malformed, and out-of-order frames. Partial
frames expire after 0.5 seconds and each stream retains at most eight partial
frames. A restarted frame index is accepted after one second without a newer
complete frame. Another UDP sender can take over after two seconds of inactivity.
Motion and point-cloud sources are tracked independently.

Frame ordering uses frame indices and local monotonic time. A sender system-clock
correction does not block newer frames or recovery after a stream restart.

`/MOVIN/Blender/Status/Request` takes `[token, replyPort]`, with a 32-digit hex
token. Replies go to the requesting IP and echo the token. The response address
is `/MOVIN/Blender/Status` and version 1 uses:

`[token, version, armatureName, armatureBoneCount, matchedBones, missingBones,
receivedMotionFPS, appliedMotionFPS, receivedPointCloudFPS, motionAge,
pointCloudAge, motionSourceMatches, pointCloudSourceMatches, error]`

Ages are seconds (`-1` before the first frame). Source flags are integer 0/1 and
compare the request's IP and source port with the stream. FPS is measured over
the last second. Status contains the latest packet error until a valid complete
frame is received. Network-controlled validation/file logging is not included
in the user add-on.

## License

Copyright 2025 MOVIN. All Rights Reserved.
