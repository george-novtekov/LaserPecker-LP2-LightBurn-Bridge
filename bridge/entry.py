"""Single-executable dispatch: GUI by default, optional headless diagnostics."""
import contextlib
import io
from pathlib import Path
import sys

def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    if '--self-test' in argv or '--scan-bluetooth' in argv:
        from .launch import main as cli_main
        from .service import default_data_dir
        report=None
        if '--report' in argv:
            index=argv.index('--report')
            if index+1>=len(argv):raise ValueError('--report requires a path')
            report=Path(argv[index+1]);del argv[index:index+2]
        output=io.StringIO()
        with contextlib.redirect_stdout(output):result=cli_main(argv)
        report=report or default_data_dir()/('self-test.json' if '--self-test' in argv else 'bluetooth-scan.json')
        report.parent.mkdir(parents=True,exist_ok=True);report.write_text(output.getvalue(),encoding='utf-8')
        if sys.stdout:print(output.getvalue(),end='')
        return result
    if '--headless' in argv:
        argv.remove('--headless')
        from .launch import main as cli_main
        return cli_main(argv)
    from .gui import main as gui_main
    return gui_main(argv)
