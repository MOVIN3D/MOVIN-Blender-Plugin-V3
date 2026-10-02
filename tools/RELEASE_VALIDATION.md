# v3.3.0 reissue validation — 2026-10-02

Validated on Windows with Blender 5.2.2 LTS using the installable ZIP and
extracted samples, in an isolated Blender user directory.

- Python receiver and skeleton diagnostics: 57 tests passed, including clock
  rollback and frame-index restart regression coverage.
- Plugin ZIP installation, enable and disable passed; installed source bytes
  match `addon/movin_blender_plugin.py` and report version 3.3.0.
- `verify_sample`, `verify_apply`, `verify_global_hips`, `verify_receiver` and
  `verify_diagnostics` passed on both packaged scenes using the installed add-on
  (10 runs, including real UDP motion, point cloud and Studio status checks).
- ZIP file lists, contents and SHA-256 checksums match their inputs. Sample
  `.blend` and `.fbx` files are unchanged from v3.3.0. Development files are excluded.

| Artifact | SHA-256 |
| --- | --- |
| `MOVIN-Blender-Plugin-v3.3.0.zip` | `64a9e0b2326c6fcd59bbfe449d16abbd89c7f7af4540a025f89380eee2f369bb` |
| `MOVIN-Blender-Samples-v3.3.0.zip` | `e1ac50be2b7955e0cc481ddaac2bac76d6d1263c32f6d7b56df174dd0c3fd7be` |

Packaged text uses LF line endings across checkouts. The final v3.3.0 ZIPs were
reinstalled and all 10 sample runs repeated after applying the reissue metadata.
The earlier release metadata, tag and assets are backed up locally. This replaces
v3.3.0 at the maintainer's explicit request; no v3.3.1 release was published.

This patch does not repeat the earlier interactive FPS benchmark, visual sample
review or mutation run. Older Blender versions, macOS and Linux were not tested.
The add-on retains its 4.3.2 minimum; prepared `.blend` samples require 5.2.2.

# v3.3.0 validation — 2026-10-01

Validated on Windows with Blender 5.2.2 LTS. This record covers the v3.3.0
artifacts listed below.

## Results

- Python receiver and skeleton diagnostics: 56 tests passed.
- Mutation checks: all 9 injected regressions were caught.
- Prepared samples: `verify_sample`, `verify_apply`, `verify_global_hips`,
  `verify_receiver`, and `verify_diagnostics` passed on both scenes (10 runs).
- Installable ZIP: installed, enabled, and disabled successfully in an isolated
  Blender user directory; installed source bytes match the release source.
- Extracted samples with the packaged add-on: saved defaults and real UDP
  receiver checks passed on both scenes (4 runs).
- Both clean sample poses rendered and were visually checked.
- Original and prepared samples retain identical bone hierarchy/rest transforms,
  connected flags, mesh coordinates/topology, skin weights, material assignments,
  Armature modifiers, and packed texture bytes.
- ZIP contents and hashes match their inputs. Tests, tools, caches, and backup
  files are excluded. `git diff --check` passed.

The motion implementation was also measured in the running Blender viewport
before the version metadata update: a 20-second motion + point cloud run had
60.0 average applied FPS after warmup, at most two queued motion frames, and
26 ms median / 32 ms maximum local receive-to-apply age. This is one local
measurement, not a performance guarantee for other scenes or computers.

## Packages

| Artifact | SHA-256 |
| --- | --- |
| `MOVIN-Blender-Plugin-v3.3.0.zip` | `7c4be06b79a72dd892c2256928857c0fdfd829e77fa63a1faa333a55bb0acde3` |
| `MOVIN-Blender-Samples-v3.3.0.zip` | `7fe4b37920f519536e36ba83a1d8c1c81c8cd1c514101302cf8bcd3524fb0f64` |

Artifacts are in `dist/v3.3.0/`, together with `manifest.json` and
`SHA256SUMS.txt`. Rebuilding after any packaged input changes requires repeating
the affected checks and updating this record.

## Compatibility limits

Blender 5.2.2 is the verified environment. The add-on's existing 4.3.2 minimum
was retained, but older Blender versions were not available for this validation.
The `.blend` samples are saved in 5.2.2; the included FBX files provide an import
path for other versions. macOS and Linux were not tested.

Pre-cleanup sample backups and their SHA-256 record are retained locally and
excluded from the release packages.
