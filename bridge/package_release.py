"""Package a freshly built app and its user guides into a Windows release ZIP."""
from pathlib import Path
import hashlib
import json
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    dist = root / 'dist'
    exe = dist / 'LP2Bridge.exe'
    manifest_path = root / 'bridge/build_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    for name, expected in manifest['source_hashes'].items():
        actual = hashlib.sha256((root / 'bridge' / name).read_bytes()).hexdigest()
        if actual != expected:
            raise SystemExit(f'Source changed after build: {name}; rebuild the EXE.')
    checksum = exe.with_suffix('.exe.sha256.txt')
    if hashlib.sha256(exe.read_bytes()).hexdigest() != checksum.read_text().split()[0]:
        raise SystemExit('EXE checksum mismatch; rebuild the EXE.')
    files = {
        'LP2Bridge.exe': exe,
        'LP2Bridge.exe.sha256.txt': checksum,
        'build_manifest.json': manifest_path,
        'README.md': root / 'README.md',
        'START-HERE.txt': root / 'docs/WINDOWS-QUICKSTART.txt',
        'start-lp2-bridge.cmd': root / 'start-lp2-bridge.cmd',
        'start-lp2-bluetooth.cmd': root / 'start-lp2-bluetooth.cmd',
    }
    files.update({f'docs/{path.name}': path for path in (root / 'docs').iterdir() if path.is_file()})
    archive = dist / 'LP2-LightBurn-Bridge-Windows.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as output:
        for name, path in sorted(files.items()):
            output.write(path, name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.zip.sha256.txt').write_text(f'{digest}  {archive.name}\n', encoding='utf-8')
    print(json.dumps({'archive': str(archive), 'version': manifest['version'],
                      'files': len(files), 'sha256': digest}, indent=2))


if __name__ == '__main__':
    main()
