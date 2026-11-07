"""LP2 LightBurn bridge: mixed jobs over USB or Bluetooth."""
import argparse
import asyncio
import datetime
import json
from pathlib import Path
import sys
import uuid

def self_test():
    from .sequence import compile_sequence
    from .job import Job
    from .raster import RasterJob,prepare_wire,parse_raster
    from .orientation import rotate_stage
    from .codec.protocol import StreamDecoder,query_status
    sample="G21G90\nG0X40Y40\nM3S50\nG1X41F3000\nM5\nG0X45Y40\nM4\nG91\nG1X.3F6000S50\nG1X-.3Y.1S0\nG1X.3S50\nM5\nG90\nG0X0Y0\nM2"
    stages=compile_sequence(sample).stages
    assert [type(x) for x in stages]==[Job,RasterJob,Job]
    assert stages[1].payload==b"\x1f\x1f"
    rotated=rotate_stage(stages[1],180)
    corrected=rotate_stage(stages[0],180,mirror_x=True)
    assert corrected.marking_vectors==(((40,60),(41,60),3000.),)
    mirrored_image=rotate_stage(stages[1],180,mirror_x=True)
    assert (mirrored_image.nx_px,mirrored_image.ny_px)==(450,598)
    assert (rotated.nx_px,rotated.ny_px)==(547,598)
    wire=prepare_wire(rotated)
    assert (wire.nx_px,wire.width_px,wire.payload)==(546,4,b'\x1f\x8f')
    varied='G21G90\nG0X20Y30\nM4\nG91\nF6000\nG1X.254S50\nG1X.254S0\nG1X.254S50\nG1X-.762Y.254S0\nG1X.254S50\nG1X.254S0\nG1X.254S50\nM5\nG90\nM2'
    variable=parse_raster(varied)
    assert variable.px==9 and abs(variable.source_pitch_mm-.254)<1e-6
    assert prepare_wire(rotate_stage(variable,180)).wire_compensated
    continuous=compile_sequence(sample,image_mode='continuous')
    assert all(isinstance(stage,Job) for stage in continuous.stages)
    assert sum(len(stage.marking_vectors) for stage in continuous.stages)==3
    from .quality import pattern,PATCHES
    diagnostic=compile_sequence(pattern(full=True),image_mode='continuous')
    assert len(PATCHES)==8 and len(diagnostic.stages)==1
    assert StreamDecoder().feed(query_status())[0].checksum_ok
    import serial,bleak
    print(json.dumps(dict(self_test='passed',version='scan-8',mixed_stages=len(stages),arbitrary_dpi=True,bidirectional_correction=True,continuous_images=True,whole_job_mirror=True,quality_pattern=True,serial_import=True,bleak_import=True,frozen=bool(getattr(sys,'frozen',False)))))
    return 0

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--transport',choices=['usb','bluetooth'],default=None)
    parser.add_argument('--port',default='COM3')
    parser.add_argument('--rotation',type=int,choices=(0,180),default=180,help='device rotation about the100x100mm bed; default180')
    parser.add_argument('--mirror-x',action=argparse.BooleanOptionalAction,default=True,help='whole-job left/right correction; default enabled')
    parser.add_argument('--ble-address',help='LP2 address; omitted = discover exactly one LP2')
    parser.add_argument('--scan-bluetooth',action='store_true')
    parser.add_argument('--self-test',action='store_true',help='offline compiler, codec and bundled dependency smoke test')
    parser.add_argument('--tcp-port',type=int,default=23230)
    parser.add_argument('--max-power',type=int,default=1000)
    parser.add_argument('--start-laser-percent',type=int,default=100)
    parser.add_argument('--read-only',action='store_true')
    parser.add_argument('--duration',type=float)
    parser.add_argument('--job-budget',type=float,default=600)
    parser.add_argument('--image-mode',choices=('continuous','bitmap'),default='continuous')
    parser.add_argument('--data-dir',type=Path,help='capture folder; default LP2BridgeData beside exe, bridge/evidence from source')
    parser.add_argument('--calibration',type=Path,help='measured calibration profile JSON')
    args=parser.parse_args(argv)
    if args.self_test:return self_test()
    if args.scan_bluetooth:
        from .ble import scan_lp2
        print(json.dumps(asyncio.run(scan_lp2()),indent=2));return 0
    if args.transport is None:
        if getattr(sys,'frozen',False) and len(sys.argv)==1:
            choice=input('LP2 transport: [1] USB  [2] Bluetooth (Enter = USB): ').strip()
            if choice not in ('','1','2'):parser.error('choose 1 or 2')
            args.transport='bluetooth' if choice=='2' else 'usb'
        else:args.transport='usb'
    if args.transport=='bluetooth' and not args.ble_address:
        from .ble import scan_lp2
        print('Discovering LP2 Bluetooth devices...',flush=True)
        devices=asyncio.run(scan_lp2())
        if len(devices)!=1:parser.error(f'found {len(devices)} candidates; use --scan-bluetooth then --ble-address ADDRESS')
        args.ble_address=devices[0]['address']
        print(f"Using {devices[0]['name']} ({args.ble_address})",flush=True)
    label='live-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
    folder=args.data_dir or (Path(sys.executable).resolve().parent/'LP2BridgeData' if getattr(sys,'frozen',False) else Path(__file__).resolve().parent/'evidence')
    options=['--port',args.port,'--rotation',str(args.rotation),'--transport',args.transport,'--tcp-port',str(args.tcp_port),'--max-power',str(args.max_power),
             '--start-laser-percent',str(args.start_laser_percent),'--label',label,'--capture-dir',str(folder),'--job-budget',str(args.job_budget),'--image-mode',args.image_mode]
    if args.ble_address:options+=['--ble-address',args.ble_address]
    options.append('--mirror-x' if args.mirror_x else '--no-mirror-x')
    if args.calibration:options+=['--calibration',str(args.calibration)]
    if args.read_only:options.append('--read-only')
    if args.duration is not None:options+=['--duration',str(args.duration)]
    print(f'LP2 bridge ({args.transport}) -> 127.0.0.1:{args.tcp_port}',flush=True)
    print(f'Device rotation: {args.rotation} degrees; LightBurn coordinates stay logical.',flush=True)
    print(f'Whole-job left/right correction: {args.mirror_x}',flush=True)
    print(f'Captures: {folder / label}-*.jsonl',flush=True)
    print('Use LP2 USB BRIDGE in LightBurn for either transport. Ctrl+C closes the bridge.',flush=True)
    from .server import main as server_main
    return server_main(options)

if __name__=='__main__':raise SystemExit(main())
