# Release preparation

The add-on's `bl_info["version"]` is the package version. The current release
family is 3.3.0, matching MOVIN Studio. Packaging does not commit, tag, upload,
or publish anything.

## Samples

Back up the existing `.blend` files before replacing them. Prepare copies into
a separate directory, using Blender 5.2.2 and the repository's current add-on:

```sh
blender --factory-startup --disable-autoexec -b samples/blend/MOVINman_V3_Sample.blend --python-exit-code 1 --python tools/prepare_sample.py -- --output /staging/MOVINman_V3_Sample.blend
blender --factory-startup --disable-autoexec -b samples/blend/Ch14_Sample.blend --python-exit-code 1 --python tools/prepare_sample.py -- --output /staging/Ch14_Sample.blend
```

This removes the sample Armature's animation, resets its pose, clears obsolete
receiver properties and captured point cloud geometry, and packs textures.
Rest bones, object transforms, skinning, materials, and connected flags are
preserved. It never overwrites the input scene. Run `verify_sample.py` and the
checks in `tests/README.md` against the prepared copies before replacing the
repository samples. Keep the same sample filenames.

## Packages

After the samples and regression tests pass:

```sh
python tools/package_release.py
```

Output is under `dist/v<version>/`:

- `MOVIN-Blender-Plugin-v<version>.zip`: installable single-file add-on.
- `MOVIN-Blender-Samples-v<version>.zip`: two `.blend` scenes, their two FBX
  models, README, and changelog.
- `manifest.json`: hashes for every packaged file and each archive.
- `SHA256SUMS.txt`: hashes for the archives and manifest.

Only explicitly listed files are packaged. Tests, preparation scripts, Git
metadata, caches, and `.blend1` backups are excluded. The packager checks archive
integrity, exact contents, and byte-for-byte equality to the source files.
Install the plugin ZIP in an isolated Blender user directory, and repeat the
sample tests from the extracted samples ZIP before publishing.

Before publication, commit the reviewed release sources and sample files, then
tag that commit `v<version>` and upload these four artifacts. The existing
`v1.0.0` tag remains unchanged. Record the Blender versions actually tested;
do not treat the declared minimum as a successful compatibility test.
