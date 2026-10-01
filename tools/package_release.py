"""Build versioned user packages from an explicit file list."""
import ast
import hashlib
import json
import zipfile
from pathlib import Path

repo = Path(__file__).resolve().parent.parent
source = repo / 'addon' / 'movin_blender_plugin.py'
tree = ast.parse(source.read_text(encoding='utf-8-sig'))
info = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'bl_info' for t in n.targets))
version = '.'.join(map(str, info['version']))
output = repo / 'dist' / ('v' + version)
output.mkdir(parents=True, exist_ok=True)
packages = {
    f'MOVIN-Blender-Plugin-v{version}.zip': {'movin_blender_plugin.py': source},
    f'MOVIN-Blender-Samples-v{version}.zip': {
        name: repo / name for name in ('README.md', 'CHANGELOG.md',
            'samples/blend/MOVINman_V3_Sample.blend', 'samples/blend/Ch14_Sample.blend',
            'samples/fbx/MOVINman_V3_Puppet.fbx', 'samples/fbx/Ch14_nonPBR.fbx')},
}
manifest = {'version': version, 'packages': {}}
for filename, files in packages.items():
    target = output / filename
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in files.items():
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, path.read_bytes())
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(files)
        assert all(archive.read(name) == path.read_bytes() for name, path in files.items())
    manifest['packages'][filename] = {
        'sha256': hashlib.sha256(target.read_bytes()).hexdigest(),
        'bytes': target.stat().st_size,
        'files': {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()},
    }
    print(filename, target.stat().st_size, 'bytes; verified', len(files), 'files')

manifest_path = output / 'manifest.json'
manifest_path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
checksums = [f"{details['sha256']}  {name}" for name, details in manifest['packages'].items()]
checksums.append(hashlib.sha256(manifest_path.read_bytes()).hexdigest() + '  manifest.json')
(output / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')
print('Release files:', output)
