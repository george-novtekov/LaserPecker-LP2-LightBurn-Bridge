"""PyInstaller entry point; no system Python required after packaging."""
import sys
from bridge.entry import main

if __name__=='__main__':
    if sys.stdout is None or sys.stderr is None:
        from bridge.service import default_data_dir
        directory=default_data_dir();directory.mkdir(parents=True,exist_ok=True)
        output=(directory/'application.log').open('a',encoding='utf-8',buffering=1)
        sys.stdout=sys.stderr=output
    try:raise SystemExit(main())
    except Exception as exc:
        print(f'LP2 bridge failed: {exc}',flush=True)
        if not any(arg in sys.argv for arg in ('--self-test','--headless','--scan-bluetooth','--gui-smoke')):
            from tkinter import messagebox
            messagebox.showerror('LP2 bridge',str(exc))
        raise SystemExit(1)
