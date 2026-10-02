# Changelog

## 3.3.1 — 2026-10-02

- Fix motion reception stopping after the sender's system clock moves backwards.
  Order frames by frame index and measure restart timeouts using monotonic time.
- Add regression coverage for clock rollback and restarted frame indices.
- Keep the existing samples and Studio compatibility: motion and point clouds
  support Studio 3.0.0+, while status and FPS feedback require Studio 3.3.0+.

Fully restart Blender after replacing the add-on to load the updated Python module.

## 3.3.0 — 2026-10-01

Align the plugin version with MOVIN Studio 3.3.0. The development add-on was
labeled 1.1.0. This is the recommended release; the legacy `v3.0.0` tag
(formerly `v1.0.0`) has been withdrawn.

- Add Studio status replies for the selected Armature, bone mapping, and
  received/applied motion and received point cloud FPS.
- Apply the streamed global Hips position with automatic axis, scene unit,
  Armature transform, and parent hierarchy compensation. Remove manual Hips
  height and translation-scale settings; old saved values are ignored.
- Validate OSC payloads, hierarchy indices, unique names, and finite transforms;
  discard incomplete, stale, and out-of-order frames.
- Release sockets and timers on Stop, file changes, and add-on disable; handle
  occupied ports and restarted senders.
- Improve point cloud decoding and mesh updates, and compensate application
  timer intervals for callback work.
- Keep at most two complete motion frames to absorb arrival jitter; replace
  the oldest when full and discard queued poses older than 50 ms.
- Prepare clean Actor and Ch14 sample scenes with packed textures and receiver
  defaults. Ship installation and sample ZIPs separately with SHA-256 checksums.

Validated on Blender 5.2.2 LTS. The add-on retains the declared 4.3.2 minimum,
but this release has not been retested on older Blender versions. Prepared
`.blend` scenes are saved in 5.2.2; FBX samples are included for other versions.

## 3.0.0 — withdrawn 2026-10-01

- The original `v1.0.0` tag was renamed to `v3.0.0`, then removed from public
  distribution because it contains known receiver issues fixed in 3.3.0.
- Use 3.3.0. The legacy Git history was archived before removing the tag;
  no source history was rewritten.
