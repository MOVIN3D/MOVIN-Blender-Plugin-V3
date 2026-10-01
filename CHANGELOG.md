# Changelog

## 3.3.0 — 2026-10-01

Align the plugin version with MOVIN Studio 3.3.0. The previously published
`v1.0.0` tag is retained; the development add-on was labeled 1.1.0.

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
