"""Build the portable Windows app in dist/ using the current Python environment."""
from pathlib import Path
import datetime
import hashlib
import json
import subprocess
import sys

def main(*,name='LP2Bridge',destination=None):
    if sys.platform != 'win32':
        raise SystemExit('Build the Windows executable on Windows with Python x64.')
    root=Path(__file__).resolve().parents[1];source=root/'bridge'
    dependencies=subprocess.check_output([sys.executable,'-m','pip','freeze'],text=True).splitlines()
    modules=('__init__.py','__main__.py','exe_entry.py','entry.py','gui.py','service.py','quality.py','calibration.py','launch.py','server.py','runtime.py','orientation.py','sequence.py','job.py','speed.py',
             'raster.py','raster_timing.py','transport.py','ble.py','codec/__init__.py','codec/protocol.py')
    manifest=dict(version='scan-8',created_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        python=sys.version,source_hashes={name:hashlib.sha256((source/name).read_bytes()).hexdigest()
          for name in modules},dependencies=dependencies)
    (source/'build_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    destination=Path(destination).resolve() if destination else root/'dist'
    destination.mkdir(parents=True,exist_ok=True)
    subprocess.run([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onefile','--windowed','--name',name,
        '--paths',str(root),'--add-data',str(source/'build_manifest.json')+';bridge',
        '--collect-submodules','bleak.backends.winrt','--collect-submodules','winrt',
        '--hidden-import','serial.tools.list_ports_windows','--distpath',str(destination),
        '--workpath',str(root/'build/pyinstaller'),'--specpath',str(root/'build'),str(source/'exe_entry.py')],cwd=root,check=True)
    path=destination/(name+'.exe');digest=hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix('.exe.sha256.txt').write_text(digest+'  '+path.name+'\n')
    print(json.dumps(dict(exe=path.name,bytes=path.stat().st_size,sha256=digest)))

if __name__=='__main__':main()
