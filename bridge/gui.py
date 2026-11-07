"""LP2 LightBurn bridge: native Tk GUI (no external dependencies)."""
import argparse
import json
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from pathlib import Path

if __package__:
    from .service import BridgeService, default_data_dir
    from .calibration import profile_from_measurements, load_profile, save_profile, Calibration
else:  # direct-run fallback for py_compile-only environments
    BridgeService = None
    default_data_dir = lambda: Path('.')
    profile_from_measurements = load_profile = save_profile = None
    Calibration = None

NAVY = '#1f3a5f'
CARD = '#ffffff'
BG = '#eef1f5'
MUTED = '#6b7480'
MONO = ('Consolas', 10)
UI = ('Segoe UI', 10)
UI_B = ('Segoe UI', 10, 'bold')
POLL_MS = 300


def _bounds_ok(x, y, w, h):
    try:
        return -32000 <= x <= 32000 and -32000 <= y <= 32000 and 640 <= w <= 4000 and 480 <= h <= 4000
    except TypeError:
        return False


class BridgeWindow:
    def __init__(self, root, service=None):
        self.root = root
        self.service = service if service is not None else BridgeService()
        self._q = queue.Queue()
        self._scan_busy = False
        self._closing = False
        self._last_seen = 0
        self._last_log_len = 0
        self._last_messages = []
        self._draft = None
        self._active = None  # profile applied to the running service
        self._last_state = None
        root.title('LP2 LightBurn Bridge (scan-8)')
        root.geometry('1100x760')
        root.minsize(960, 680)
        root.configure(bg=BG)
        root.option_add('*Font',UI)
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('TFrame',background=BG)
        style.configure('TButton',font=UI,padding=(10,6))
        style.configure('TNotebook.Tab',font=UI_B,padding=(18,9))
        style.configure('TCombobox',padding=4)
        style.configure('Stop.TButton',foreground='#b42318',font=UI_B)

        self._prefs_path = Path(default_data_dir()) / 'gui-settings.json'
        self._build()
        self._load_prefs()
        self._refresh_ports()
        root.protocol('WM_DELETE_WINDOW', self._on_close)
        root.after(POLL_MS, self._poll)

    # ---------- layout ----------
    def _build(self):
        header = tk.Frame(self.root, bg=NAVY, height=64)
        header.pack(fill='x')
        header.pack_propagate(False)
        tk.Label(header, text='LP2 LightBurn Bridge', bg=NAVY, fg='white',
                 font=('Segoe UI', 15, 'bold')).pack(side='left', padx=20)
        self._hdr_sub = tk.Label(header, text='LightBurn profile: LP2 USB BRIDGE  ->  127.0.0.1:23230',
                                 bg=NAVY, fg='#b8c6da', font=UI)
        self._hdr_sub.pack(side='left', padx=8)
        self._header_stop=ttk.Button(header,text='Stop job',style='Stop.TButton',command=self._stop_job,state='disabled')
        self._header_stop.pack(side='right',padx=16)

        self._status_var = tk.StringVar(value='idle')
        st = tk.Frame(self.root, bg='#d8dee7', height=30)
        st.pack(fill='x', side='bottom')
        st.pack_propagate(False)
        tk.Label(st, textvariable=self._status_var, bg='#d8dee7', fg='#22303f',
                 font=UI).pack(side='left', padx=12)
        tk.Label(st, text='positions are host estimates', bg='#d8dee7', fg=MUTED,
                 font=UI).pack(side='right', padx=12)
        nb = ttk.Notebook(self.root)
        self._notebook=nb
        nb.pack(fill='both', expand=True, padx=12, pady=(8, 12))
        self._tab_conn = ttk.Frame(nb, padding=14)
        self._tab_cal=ttk.Frame(nb)
        scroll=tk.Canvas(self._tab_cal,bg=BG,highlightthickness=0)
        bar=ttk.Scrollbar(self._tab_cal,orient='vertical',command=scroll.yview)
        scroll.configure(yscrollcommand=bar.set);bar.pack(side='right',fill='y');scroll.pack(side='left',fill='both',expand=True)
        content=ttk.Frame(scroll,padding=14);item=scroll.create_window((0,0),window=content,anchor='nw')
        content.bind('<Configure>',lambda e:scroll.configure(scrollregion=scroll.bbox('all')))
        scroll.bind('<Configure>',lambda e:scroll.itemconfigure(item,width=e.width))
        self._cal_scroll=scroll
        def wheel(event):
            if nb.select()==str(self._tab_cal):scroll.yview_scroll(-int(event.delta/120),'units')
        self.root.bind('<MouseWheel>',wheel)

        self._tab_log = ttk.Frame(nb, padding=14)
        self._tab_quality=ttk.Frame(nb,padding=14)
        nb.add(self._tab_conn, text='Connection')
        nb.add(self._tab_cal, text='Calibration')
        nb.add(self._tab_quality,text='Print quality')
        nb.add(self._tab_log, text='Logs')
        self._build_connection(self._tab_conn)
        self._build_calibration(content)
        self._build_quality(self._tab_quality)
        self._build_logs(self._tab_log)

    def _build_quality(self,parent):
        body=self._card(parent,'Banding test — begin with one patch')
        tk.Label(body,text='Start with patch 1 on fresh scrap at 1% and 100 mm/s. Check it before trying all eight.\n'
            'Every patch uses the same 0.125 mm spacing. The former denser-row test has been removed.',
            bg=CARD,fg=MUTED,font=UI,justify='left',wraplength=840).pack(anchor='w')
        row=tk.Frame(body,bg=CARD);row.pack(fill='x',pady=(10,6))
        self._quality_power=tk.StringVar(value='1');self._quality_speed=tk.StringVar(value='100')
        for title,var in (('Test power % (1..30)',self._quality_power),('Speed (50..202 mm/s)',self._quality_speed)):
            tk.Label(row,text=title,bg=CARD,font=UI).pack(side='left',padx=(0,6))
            ttk.Entry(row,textvariable=var,width=8).pack(side='left',padx=(0,18))
        self._quality_btn=ttk.Button(row,text='Engrave patch 1',command=self._engrave_quality,state='disabled')
        self._quality_btn.pack(side='left')
        self._quality_all_btn=ttk.Button(row,text='Engrave all 8',command=lambda:self._engrave_quality(full=True),state='disabled')
        self._quality_all_btn.pack(side='left',padx=8)
        self._quality_swap=tk.BooleanVar(value=False)
        self._quality_swap_btn=ttk.Checkbutton(body,text='Swap horizontal / vertical scans; keep patch numbers and positions',
            variable=self._quality_swap,command=self._draw_quality_diagram)
        self._quality_swap_btn.pack(anchor='w',pady=4)
        self._quality_canvas=tk.Canvas(body,width=840,height=230,bg='#fafbfc',highlightthickness=0)
        self._quality_canvas.pack(anchor='w',pady=6);self._draw_quality_diagram()
        tk.Label(body,text='Both rows now have equal nominal exposure per area: same power, speed and spacing.\n'
            'The second row repeats the scan-direction comparison at another field position.\n'
            'Use fresh scrap; stop if it chars or perforates. Power in Logs is a setting, not an optical measurement.',
            bg=CARD,fg=MUTED,font=UI,justify='left',wraplength=840).pack(anchor='w',pady=(4,0))

    def _draw_quality_diagram(self):
        cv=self._quality_canvas;cv.delete('all')
        axes=('Vertical','Vertical','Horizontal','Horizontal') if self._quality_swap.get() else ('Horizontal','Horizontal','Vertical','Vertical')
        for col,axis in enumerate(axes):
            x=110+col*180
            cv.create_text(x+60,14,text=axis+(' alternating' if col%2 else ' forward'),font=('Segoe UI',9),fill=NAVY)
            for rowidx in range(2):
                y=45+rowidx*95;cv.create_rectangle(x,y,x+120,y+58,fill='#dee7f0',outline=NAVY)
                cv.create_text(x+60,y+28,text=str(rowidx*4+col+1),font=('Segoe UI',16,'bold'),fill=NAVY)
                cv.create_text(x+60,y+74,text='.125 mm interval',font=('Segoe UI',9),fill=MUTED)

    def _engrave_quality(self,*,full=False):
        try:
            self.service.submit_quality_pattern(int(self._quality_power.get()),float(self._quality_speed.get()),full=full,swap_axes=self._quality_swap.get())
            self._set_status('quality test queued; keep the numbered sample for comparison')
        except Exception as exc:self._error('Quality test rejected',exc)

    def _card(self, parent, title):
        outer = tk.Frame(parent, bg=CARD, bd=0, highlightthickness=1,
                         highlightbackground='#d3dae3')
        outer.pack(fill='x', pady=(0, 12))
        tk.Label(outer, text=title, bg=CARD, fg='#22303f', font=UI_B).pack(
            anchor='w', padx=14, pady=(10, 4))
        body = tk.Frame(outer, bg=CARD)
        body.pack(fill='x', padx=14, pady=(2, 12))
        return body

    def _build_connection(self, parent):
        body = self._card(parent, 'Transport')
        row = tk.Frame(body, bg=CARD); row.pack(fill='x')
        tk.Label(row, text='Transport', bg=CARD, font=UI).grid(row=0, column=0, sticky='w', pady=3)
        self._transport = tk.StringVar(value='usb')
        rb1 = ttk.Radiobutton(row, text='USB', value='usb', variable=self._transport,
                              command=self._transport_changed)
        rb2 = ttk.Radiobutton(row, text='Bluetooth', value='bluetooth', variable=self._transport,
                              command=self._transport_changed)
        rb1.grid(row=0, column=1, sticky='w', padx=12); rb2.grid(row=0, column=2, sticky='w')
        tk.Label(row, text='Port', bg=CARD, font=UI).grid(row=1, column=0, sticky='w', pady=3)
        self._port_var = tk.StringVar()
        self._port_cb = ttk.Combobox(row, textvariable=self._port_var, width=28, values=[])
        self._port_cb.grid(row=1, column=1, columnspan=2, sticky='w', padx=12)
        refresh=ttk.Button(row,text='Refresh',command=self._refresh_ports)
        refresh.grid(row=1,column=3,sticky='w',padx=10)
        tk.Label(row, text='BLE address', bg=CARD, font=UI).grid(row=2, column=0, sticky='w', pady=3)
        self._ble_var = tk.StringVar()
        self._ble_cb = ttk.Combobox(row, textvariable=self._ble_var, width=40, values=[])
        self._ble_cb.grid(row=2, column=1, columnspan=2, sticky='w', padx=12)
        self._ble_scan_btn = ttk.Button(row, text='Scan', command=self._scan_bluetooth)
        self._ble_scan_btn.grid(row=2,column=3,sticky='w',padx=10)
        tk.Label(row, text='TCP port', bg=CARD, font=UI).grid(row=3, column=0, sticky='w', pady=3)
        self._tcp_var = tk.StringVar(value='23230')
        tcp_entry=ttk.Entry(row,textvariable=self._tcp_var,width=10)
        tcp_entry.grid(row=3,column=1,sticky='w',padx=12)
        self._conn_widgets=[rb1,rb2,self._port_cb,self._ble_cb,self._ble_scan_btn,refresh,tcp_entry]
        row2 = tk.Frame(body, bg=CARD); row2.pack(fill='x', pady=(8, 0))
        tk.Label(row2, text='Rotation', bg=CARD, font=UI).grid(row=0, column=0, sticky='w')
        self._rotation = tk.StringVar(value='180')
        ttk.Combobox(row2, textvariable=self._rotation, values=('180', '0'), width=6,
                     state='readonly').grid(row=0, column=1, sticky='w', padx=12)
        tk.Label(row2, text='Max power %', bg=CARD, font=UI).grid(row=0, column=2, sticky='w', padx=(24, 0))
        self._maxpower_var = tk.StringVar(value='100')
        ttk.Entry(row2, textvariable=self._maxpower_var, width=8).grid(row=0, column=3, sticky='w', padx=12)
        tk.Label(row2, text='Job budget s', bg=CARD, font=UI).grid(row=0, column=4, sticky='w', padx=(24, 0))
        self._budget_var = tk.StringVar(value='600')
        ttk.Entry(row2, textvariable=self._budget_var, width=8).grid(row=0, column=5, sticky='w', padx=12)
        self._conn_widgets.extend(w for w in row2.winfo_children() if isinstance(w,(ttk.Combobox,ttk.Entry)))
        self._mirror_x=tk.BooleanVar(value=True)
        mirror_cb=ttk.Checkbutton(row2,text='Correct left/right mirror (whole job)',variable=self._mirror_x)
        mirror_cb.grid(row=1,column=0,columnspan=6,sticky='w',pady=(6,0))
        self._conn_widgets.append(mirror_cb)
        row3=tk.Frame(body,bg=CARD);row3.pack(fill='x',pady=(8,0))
        tk.Label(row3,text='Image scan',bg=CARD,font=UI).pack(side='left')
        self._image_mode=tk.StringVar(value='Continuous (quality)')
        image_cb=ttk.Combobox(row3,textvariable=self._image_mode,width=30,state='readonly',
            values=('Continuous (quality)','Native bitmap (fast transfer)'))
        image_cb.pack(side='left',padx=12);self._conn_widgets.append(image_cb)
        tk.Label(row3,text='Continuous preserves scan spacing; Bluetooth uploads take longer.',bg=CARD,fg=MUTED,font=UI).pack(side='left')
        btns = tk.Frame(body, bg=CARD); btns.pack(fill='x', pady=(12, 0))
        self._connect_btn = ttk.Button(btns, text='Connect', command=self._connect)
        self._connect_btn.pack(side='left')
        self._disconnect_btn = ttk.Button(btns, text='Disconnect', command=self._disconnect, state='disabled')
        self._disconnect_btn.pack(side='left', padx=8)
        self._stopjob_btn = ttk.Button(btns, text='Stop job', command=self._stop_job, state='disabled')
        self._stopjob_btn.pack(side='left')
        status=self._card(parent,'Device and current job')
        self._device_state=tk.Label(status,text='Disconnected',bg=CARD,fg=NAVY,font=('Segoe UI',20,'bold'))
        self._device_state.pack(anchor='w')
        self._device_detail=tk.Label(status,text='Connect to the laser to begin.',bg=CARD,fg=MUTED,font=UI,wraplength=900,justify='left')
        self._device_detail.pack(anchor='w',pady=(3,6))
        self._progress=ttk.Progressbar(status,maximum=100);self._progress.pack(fill='x',pady=(0,3))
        tk.Label(parent, bg=BG, fg=MUTED, font=UI, justify='left',
                 text='LightBurn profile stays LP2 USB BRIDGE with endpoint 127.0.0.1:23230 for both transports.\n'
                      'Bluetooth jobs require USB unplugged and the normal external power supply connected.'
                 ).pack(anchor='w')

    def _build_calibration(self, parent):
        body = self._card(parent, 'Measurements (mm)')
        tk.Label(body, bg=CARD, fg=MUTED, font=UI, justify='left',
                 text='Engrave the baseline, then enter its six caliper spans below. Center spans measure shrink; outer spans measure bow.\n'
                      'Use the same focus and head height. Verify on fresh material; enter baseline measurements only.'
                 ).pack(anchor='w', pady=(0, 8))
        grid = tk.Frame(body, bg=CARD); grid.pack(fill='x')
        self._size_var = tk.StringVar(value='80')
        tk.Label(grid, text='Target size (20..80)', bg=CARD, font=UI).grid(row=0, column=0, sticky='w', pady=3)
        ttk.Entry(grid, textvariable=self._size_var, width=10).grid(row=0, column=1, sticky='w', padx=10)
        self._cal_entries = {}
        for i, key in enumerate(('Wtop', 'Wmiddle', 'Wbottom')):
            tk.Label(grid, text=key, bg=CARD, font=UI).grid(row=1 + i, column=0, sticky='w', pady=3)
            v = tk.StringVar(); self._cal_entries[key] = v
            ttk.Entry(grid, textvariable=v, width=10).grid(row=1 + i, column=1, sticky='w', padx=10)
        for i, key in enumerate(('Hleft', 'Hmiddle', 'Hright')):
            tk.Label(grid, text=key, bg=CARD, font=UI).grid(row=1 + i, column=2, sticky='w', padx=(30, 0))
            v = tk.StringVar(); self._cal_entries[key] = v
            ttk.Entry(grid, textvariable=v, width=10).grid(row=1 + i, column=3, sticky='w', padx=10)
        tk.Label(grid, text='Pattern power % (1..100)', bg=CARD, font=UI).grid(row=0, column=4, sticky='w', padx=(30, 0))
        self._power_var = tk.StringVar(value='5')
        ttk.Entry(grid, textvariable=self._power_var, width=10).grid(row=0, column=5, sticky='w', padx=10)
        tk.Label(grid, text='Speed mm/s (2..202)', bg=CARD, font=UI).grid(row=1, column=4, sticky='w', padx=(30, 0))
        self._speed_var = tk.StringVar(value='50')
        ttk.Entry(grid, textvariable=self._speed_var, width=10).grid(row=1, column=5, sticky='w', padx=10)
        btns = tk.Frame(body, bg=CARD); btns.pack(fill='x', pady=(10, 0))
        self._baseline_btn=ttk.Button(btns,text='1. Engrave baseline',command=lambda:self._engrave(False));self._baseline_btn.pack(side='left')
        ttk.Button(btns,text='2. Calculate correction',command=self._calculate).pack(side='left',padx=6)
        ttk.Button(btns, text='Save profile', command=self._save_profile).pack(side='left')
        ttk.Button(btns, text='Load profile', command=self._load_profile).pack(side='left', padx=6)
        self._apply_btn=ttk.Button(btns,text='3. Apply profile',command=self._apply_profile);self._apply_btn.pack(side='left')
        self._check_btn=ttk.Button(btns,text='4. Engrave check',command=lambda:self._engrave(True));self._check_btn.pack(side='left',padx=6)
        ttk.Button(body,text='Disable correction',command=self._disable_profile).pack(anchor='e',pady=(8,0))
        lower = tk.Frame(parent, bg=BG); lower.pack(fill='both', expand=True)
        left = tk.Frame(lower, bg=CARD, highlightthickness=1, highlightbackground='#d3dae3')
        left.pack(side='left', fill='both', expand=True, padx=(0, 12))
        tk.Label(left, text='Model preview (modeled, not measured)', bg=CARD, font=UI_B).pack(anchor='w', padx=14, pady=(10, 2))
        self._summary = tk.Label(left, text='Active profile: uncalibrated (no profile applied)',
                                 bg=CARD, fg=MUTED, font=UI, justify='left', anchor='nw',wraplength=380)
        self._summary.pack(anchor='w', padx=14, pady=(0, 6))
        self._canvas = tk.Canvas(left, width=300, height=240, bg='#fafbfc',
                                 highlightthickness=1, highlightbackground='#d3dae3')
        self._canvas.pack(padx=14, pady=(0, 14))
        right = tk.Frame(lower, bg=CARD, highlightthickness=1, highlightbackground='#d3dae3')
        right.pack(side='left', fill='y')
        tk.Label(right, text='Diagram', bg=CARD, font=UI_B).pack(anchor='w', padx=14, pady=(10, 2))
        self._diagram = tk.Canvas(right, width=340, height=240, bg='#fafbfc',
                                  highlightthickness=1, highlightbackground='#d3dae3')
        self._diagram.pack(padx=14, pady=(0, 14))
        tk.Label(right,text='Measure the baseline spans end to end. If correction exceeds the available field, use smaller artwork and a smaller check pattern.',bg=CARD,fg=MUTED,font=UI,wraplength=320,justify='left').pack(padx=14,pady=(0,12))
        self._draw_diagram()

    def _build_logs(self, parent):
        self._log = tk.Text(parent, bg='#10161f', fg='#d5dee9', insertbackground='white',
                            font=MONO, state='disabled', wrap='none')
        self._log.pack(fill='both', expand=True)

    # ---------- prefs ----------
    def _load_prefs(self):
        try:
            prefs = json.loads(self._prefs_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return
        if not isinstance(prefs,dict):return
        g = prefs.get('geometry')
        if isinstance(g, dict) and _bounds_ok(g.get('x'), g.get('y'), g.get('width', 1100), g.get('height', 760)):
            self.root.geometry(f"{g['width']}x{g['height']}+{g['x']}+{g['y']}")
        prof = prefs.get('calibration_profile')
        if isinstance(prof,dict):
            try:
                Calibration.from_profile(prof);self._active=prof;self._populate_measurements(prof);self._render_summary()
            except Exception:self._active=None
        for key, var in (('transport', self._transport), ('port', self._port_var),
                         ('ble_address', self._ble_var), ('tcp_port', self._tcp_var),
                         ('rotation', self._rotation), ('max_power_percent', self._maxpower_var),
                         ('job_budget', self._budget_var)):
            val = prefs.get(key)
            if isinstance(val, (str, int)) and str(val):
                var.set(str(val))
        self._image_mode.set('Native bitmap (fast transfer)' if prefs.get('image_mode')=='bitmap' else 'Continuous (quality)')
        if type(prefs.get('mirror_x')) is bool:self._mirror_x.set(prefs['mirror_x'])

    def _save_prefs(self):
        prefs = {}
        try:
            prefs = json.loads(self._prefs_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        if not isinstance(prefs,dict):prefs={}
        try:
            prefs['geometry'] = {'x': self.root.winfo_x(), 'y': self.root.winfo_y(),
                                 'width': self.root.winfo_width(), 'height': self.root.winfo_height()}
        except tk.TclError:
            pass
        prefs['transport'] = self._transport.get()
        prefs['port'] = self._port_var.get()
        prefs['ble_address'] = self._ble_var.get()
        prefs['tcp_port'] = self._tcp_var.get()
        prefs['rotation'] = self._rotation.get()
        prefs['mirror_x'] = self._mirror_x.get()
        prefs['max_power_percent'] = self._maxpower_var.get()
        prefs['job_budget'] = self._budget_var.get()
        prefs['image_mode']='bitmap' if self._image_mode.get().startswith('Native') else 'continuous'
        prefs['calibration_profile'] = self._active
        try:
            self._prefs_path.parent.mkdir(parents=True, exist_ok=True)
            self._prefs_path.write_text(json.dumps(prefs, indent=2), encoding='utf-8')
        except OSError as exc:
            self._set_status(f'prefs not saved: {exc}')

    # ---------- connection ----------
    def _transport_changed(self):
        ble = self._transport.get() == 'bluetooth'
        self._port_cb.configure(state='disabled' if ble else 'readonly')
        self._ble_cb.configure(state='normal' if ble else 'disabled')

    def _refresh_ports(self):
        try:
            ports = self.service.scan_ports()
        except Exception as exc:
            self._error('Port scan failed', exc)
            return
        values = [p['port'] for p in ports]
        self._port_cb.configure(values=values)
        if values and not self._port_var.get():
            self._port_var.set(values[0])

    def _scan_bluetooth(self):
        if self._scan_busy:
            return
        self._scan_busy = True
        self._ble_scan_btn.configure(state='disabled')
        self._set_status('Bluetooth scan running...')

        def worker():
            try:
                devices = self.service.scan_bluetooth()
                self._q.put(('ble_scan', devices))
            except Exception as exc:
                self._q.put(('scan_error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def _config(self):
        cfg = {'transport': self._transport.get(), 'port': self._port_var.get(),
               'ble_address': self._ble_var.get().strip() or None,
               'tcp_port': int(self._tcp_var.get() or 23230),
               'rotation': int(self._rotation.get() or 180),
               'mirror_x': self._mirror_x.get(),
               'power_cap': int(round(float(self._maxpower_var.get() or 100) * 10)),
               'start_laser_percent': 100,
               'job_budget': float(self._budget_var.get() or 600),
               'image_mode':'bitmap' if self._image_mode.get().startswith('Native') else 'continuous',
               'calibration_profile': self._active}
        return cfg

    def _connect(self):
        try:
            cfg=self._config()
            if not 1<=cfg['tcp_port']<=65535:raise ValueError('TCP port must be1..65535')
            if not 1<=cfg['job_budget']<=7200:raise ValueError('job budget must be1..7200seconds')
            self._hdr_sub.configure(text='LightBurn: LP2 USB BRIDGE  /  127.0.0.1:'+str(cfg['tcp_port']))
            self.service.start(cfg)
        except Exception as exc:
            self._error('Connect failed', exc)

    def _disconnect(self):
        try:
            self.service.stop()
        except Exception as exc:
            self._error('Disconnect failed', exc)

    def _stop_job(self):
        try:
            self.service.cancel_job()
        except Exception as exc:
            self._error('Cancel failed', exc)

    # ---------- calibration ----------
    def _measurements_ready(self):
        return all(v.get().strip() for v in self._cal_entries.values())

    def _engrave(self, corrected):
        try:
            power = int(self._power_var.get())
            if not 1 <= power <= 100:
                raise ValueError('pattern power must be 1..100 %')
            self.service.submit_pattern(self._size(), power, self._speed(), corrected)
            self._set_status(('corrected check' if corrected else 'baseline') + ' pattern queued')
        except Exception as exc:
            self._error('Enqueue rejected', exc)

    def _size(self):
        s = float(self._size_var.get() or 80)
        if not 20 <= s <= 80:
            raise ValueError('target size must be 20..80 mm')
        return s

    def _speed(self):
        s = float(self._speed_var.get() or 50)
        if not 2 <= s <= 202:
            raise ValueError('speed must be 2..202 mm/s')
        return s

    def _calculate(self):
        if not self._measurements_ready():
            self._set_status('fill all six caliper measurements first')
            return
        try:
            e = {k: float(v.get()) for k, v in self._cal_entries.items()}
            draft = profile_from_measurements(
                self._size(),
                (e['Wtop'], e['Wmiddle'], e['Wbottom']),
                (e['Hleft'], e['Hmiddle'], e['Hright']))
        except Exception as exc:
            self._error('Calibration fit failed', exc)
            return
        self._draft = draft
        self._render_summary()
        self._set_status('draft calculated; Apply forwards it to the service while idle')

    def _save_profile(self):
        if self._draft is None and self._active is None:
            self._set_status('nothing to save; calculate a correction first')
            return
        path = filedialog.asksaveasfilename(defaultextension='.json', filetypes=[('JSON', '*.json')],
                                            initialdir=str(Path(default_data_dir()) / 'calibrationprofiles'))
        if not path:
            return
        try:
            save_profile(Path(path),self._draft or self._active)
            self._set_status(f'saved {path}')
        except Exception as exc:
            self._error('Save failed', exc)

    def _load_profile(self):
        path = filedialog.askopenfilename(filetypes=[('JSON', '*.json')],
                                         initialdir=str(Path(default_data_dir()) / 'calibrationprofiles'))
        if not path:
            return
        try:
            self._draft=load_profile(Path(path))
            self._populate_measurements(self._draft)
            self._render_summary()
            self._set_status(f'loaded {path} (draft; Apply to forward)')
        except Exception as exc:
            self._error('Load failed', exc)

    def _apply_profile(self):
        if self._draft is None:
            self._set_status('no draft to apply')
            return
        try:
            self.service.apply_profile(self._draft)
            self._active = self._draft
            self._render_summary()
            self._save_prefs()
            self._set_status('profile applied')
        except Exception as exc:
            self._error('Apply rejected (service must be idle)', exc)

    def _populate_measurements(self,profile):
        self._size_var.set(str(profile['size_mm']))
        m=profile.get('measurements',{})
        for keys,values in ((('Wtop','Wmiddle','Wbottom'),m.get('widths',[])),(('Hleft','Hmiddle','Hright'),m.get('heights',[]))):
            for k,v in zip(keys,values):self._cal_entries[k].set(str(v))

    def _disable_profile(self):
        try:
            self.service.apply_profile(None);self._active=None;self._save_prefs();self._render_summary()
        except Exception as exc:self._error('Disable correction',exc)

    def _render_summary(self):
        prof=self._draft or self._active
        if not prof:
            self._summary.configure(text='Uncalibrated. Engrave the baseline and enter its measured spans.')
            self._draw_preview(None);return
        c=prof['coefficients'];size=prof['size_mm']
        label='Active' if prof==self._active else 'Draft - not applied'
        text=(f'{label}: {prof.get("name","Measured profile")}\n'
              f'Command scale X {1/c["ax"]:.4f}x / Y {1/c["ay"]:.4f}x\n'
              f'Baseline side bow {c["bx"]*size/2:+.3f}mm; top/bottom bow {c["by"]*size/2:+.3f}mm.\n'
              'Symmetric scale and edge-bow model. Verify the corrected pattern.')
        if prof.get('warnings'):text+='\n'+'\n'.join(prof['warnings'])
        self._summary.configure(text=text);self._draw_preview(prof)

    def _draw_preview(self,prof):
        cv=self._canvas;cv.delete('all');model=Calibration.from_profile(prof)
        curves=[]
        for i in range(7):
            for vertical in (True,False):
                line=[(i*100/6,j*100/24) if vertical else (j*100/24,i*100/6) for j in range(25)]
                for func,color,dash in ((model.forward,'#2f6fb5',()),(model.inverse,'#c07a2e',(3,2))):
                    try:curves.append(([func(*p) for p in line],color,dash))
                    except Exception:pass
        allpoints=[p for line,_,_ in curves for p in line]+[(0,0),(100,100)]
        minx=min(p[0] for p in allpoints);maxx=max(p[0] for p in allpoints)
        miny=min(p[1] for p in allpoints);maxy=max(p[1] for p in allpoints)
        scale=min(260/max(1,maxx-minx),175/max(1,maxy-miny))
        cx,cy=(minx+maxx)/2,(miny+maxy)/2
        def pixel(p):return (150+(p[0]-cx)*scale,104-(p[1]-cy)*scale)
        a,b=pixel((0,0)),pixel((100,100));cv.create_rectangle(*a,*b,outline='#adb5bd')
        for line,color,dash in curves:cv.create_line([v for p in line for v in pixel(p)],fill=color,dash=dash)
        cv.create_text(150,216,text='Blue: measured model',fill='#2f6fb5',font=('Segoe UI',9))
        cv.create_text(150,232,text='Orange: corrected commands',fill='#986020',font=('Segoe UI',9))

    def _draw_diagram(self):
        cv=self._diagram;cv.delete('all');lo,hi=64,236;mid=150
        for y,name in ((35,'Wtop'),(115,'Wmiddle'),(195,'Wbottom')):
            cv.create_line(lo,y,hi,y,fill=NAVY,width=2)
            cv.create_text(292,y,text=name,fill=NAVY,font=UI)
        for x,name in ((lo,'Hleft'),(mid,'Hmiddle'),(hi,'Hright')):
            cv.create_line(x,35,x,195,fill=NAVY,width=2)
            cv.create_text(x,218,text=name,fill=NAVY,font=UI)
        cv.create_text(150,12,text='Measure end to end along each span',fill=MUTED,font=('Segoe UI',9))

    # ---------- logs / poll ----------
    def _append_log(self, line):
        self._log.configure(state='normal')
        self._log.insert('end', line + '\n')
        if int(self._log.index('end-1c').split('.')[0])>500:self._log.delete('1.0','2.0')
        self._log.see('end')
        self._log.configure(state='disabled')

    def _set_status(self, text):
        self._status_var.set(text)

    def _error(self, title, exc):
        self._set_status(f'{title}: {exc}')
        messagebox.showerror(title, str(exc))

    def _poll(self):
        if self._closing:
            if not self.service.snapshot().get('running'):self._finish_close()
            else:self.root.after(POLL_MS,self._poll)
            return
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == 'ble_scan':
                    self._ble_scan_btn.configure(state='normal')
                    self._scan_busy = False
                    addrs = [d['address'] for d in payload]
                    self._ble_cb.configure(values=addrs)
                    if addrs and not self._ble_var.get():
                        self._ble_var.set(addrs[0])
                    self._set_status(f'Bluetooth scan: {len(addrs)} candidate(s)')
                elif kind == 'scan_error':
                    self._ble_scan_btn.configure(state='normal')
                    self._scan_busy = False
                    self._error('Bluetooth scan failed', payload)
        except queue.Empty:
            pass
        try:
            snap = self.service.snapshot()
        except Exception:
            snap = None
        if snap:
            state = snap.get('state', 'idle')
            running = bool(snap.get('running'))
            parts = [f'state: {state}']
            if snap.get('starting'):
                parts.append('starting')
            if snap.get('stopping'):
                parts.append('stopping')
            if snap.get('client_connected'):
                parts.append('LightBurn connected')
            pos = snap.get('position')
            if pos:
                parts.append(f'pos ~({pos[0]:.1f},{pos[1]:.1f})')
            if snap.get('progress') is not None:
                parts.append(f'progress {snap["progress"]}%')
            if snap.get('temperature') is not None:
                parts.append(f'temp {snap["temperature"]}')
            if snap.get('error'):
                parts.append(f'error: {snap["error"]}')
            self._set_status('  |  '.join(parts))
            if state != self._last_state:
                self._last_state = state
            self._connect_btn.configure(state='disabled' if running or snap.get('starting') else 'normal')
            self._disconnect_btn.configure(state='normal' if running else 'disabled')
            self._stopjob_btn.configure(state='normal' if running else 'disabled')
            self._header_stop.configure(state='normal' if snap.get('connected') else 'disabled')
            self._device_state.configure(text=('Connected - '+state) if snap.get('connected') else ('Connecting...' if snap.get('starting') else ('Connection error' if snap.get('error') else 'Disconnected')))
            self._device_detail.configure(text=snap.get('error') or ('LightBurn: '+('connected' if snap.get('client_connected') else 'waiting')+'   |   Profile: '+snap.get('profile_name','Uncalibrated')))
            self._progress.configure(value=snap.get('progress') or 0)
            for w in self._conn_widgets:
                w.configure(state='disabled' if running else ('readonly' if isinstance(w,ttk.Combobox) else 'normal'))
            if not running:self._transport_changed()
            idle=snap.get('connected') and state=='idle'
            self._baseline_btn.configure(state='normal' if idle else 'disabled')
            self._quality_btn.configure(state='normal' if idle else 'disabled')
            self._quality_all_btn.configure(state='normal' if idle else 'disabled')
            self._quality_swap_btn.configure(state='normal' if idle or not running else 'disabled')
            self._check_btn.configure(state='normal' if idle and self._active else 'disabled')
            self._apply_btn.configure(state='normal' if self._draft and (idle or not running) else 'disabled')
        msgs=(snap or {}).get('messages') or []
        added=msgs
        if self._last_messages:
            try:added=msgs[msgs.index(self._last_messages[-1])+1:]
            except ValueError:pass
        for line in added:self._append_log(line)
        self._last_messages=list(msgs)
        self.root.after(POLL_MS, self._poll)

    # ---------- close ----------
    def _on_close(self):
        if self._closing:
            return
        self._closing = True
        self._set_status('stopping service...')
        try:
            self.service.stop()
        except Exception as exc:
            self._error('Stop failed', exc)
        try:
            snap = self.service.snapshot()
        except Exception:
            snap = {}
        if not snap.get('running'):
            self._finish_close()
        # else: poll loop finishes the close once running is False

    def _finish_close(self):
        self._save_prefs()
        self.root.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--gui',action='store_true',help=argparse.SUPPRESS)
    ap.add_argument('--connect',action='store_true',help='connect using the selected settings without engraving')
    ap.add_argument('--transport',choices=('usb','bluetooth'))
    ap.add_argument('--port')
    ap.add_argument('--ble-address')
    ap.add_argument('--rotation',type=int,choices=(0,180))
    ap.add_argument('--mirror-x',action=argparse.BooleanOptionalAction,default=None)
    ap.add_argument('--gui-smoke', type=float, default=None,
                    help='show the window for N seconds without connecting, then auto-close')
    args = ap.parse_args(argv)
    root = tk.Tk()
    win = BridgeWindow(root)
    for value,var in ((args.transport,win._transport),(args.port,win._port_var),(args.ble_address,win._ble_var),(args.rotation,win._rotation)):
        if value is not None:var.set(str(value))
    win._transport_changed()
    if args.mirror_x is not None:win._mirror_x.set(args.mirror_x)
    if args.connect:root.after(250,win._connect)
    if args.gui_smoke is not None:
        root.after(max(1, int(args.gui_smoke * 1000)), win._on_close)
    root.mainloop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
