# Save Porter window. all the actual save logic is in saveporter.py
import io, re, shutil, threading, time, tkinter as tk
import xml.etree.ElementTree as ET
from pathlib import Path
from tkinter import messagebox, filedialog

from PIL import Image, ImageDraw, ImageTk

import saveporter as engine

BG, SURFACE, CARD, CARD_HI, LINE = '#0a0f18', '#101725', '#141d2d', '#1a2538', '#243149'
INK, DIM, FAINT = '#eef0f4', '#8d98ab', '#4d5a70'
CYAN, CORAL, AMBER, GREEN = '#33d3c6', '#ff6f5e', '#ffbf47', '#6fd98a'
F_UI, F_MONO = 'Segoe UI', 'Consolas'
COVER_W, COVER_H = 58, 80
MAX_CARDS = 250

CONSOLES = {
    'psx': 'PlayStation', 'ps2': 'PlayStation 2', 'ps3': 'PlayStation 3', 'psp': 'PSP',
    'psvita': 'PS Vita', 'gba': 'Game Boy Advance', 'gb': 'Game Boy', 'gbc': 'Game Boy Color',
    'snes': 'Super Nintendo', 'nes': 'NES', 'n64': 'Nintendo 64', 'nds': 'Nintendo DS',
    'gamecube': 'GameCube', 'wii': 'Wii', 'switch': 'Switch', 'megadrive': 'Mega Drive',
    'dreamcast': 'Dreamcast', 'saturn': 'Saturn', 'xbox': 'Xbox', 'xbox360': 'Xbox 360',
    'mame': 'Arcade (MAME)', 'naomi': 'NAOMI', 'naomi2': 'NAOMI 2', 'atomiswave': 'Atomiswave',
    'retroarch': 'RetroArch (console unknown)', 'fbneo': 'FinalBurn Neo', 'hbmame': 'HBMAME', 'dolphin': 'Dolphin system', 'n3ds': '3DS',
}


def when(t):
    d = time.localtime(t)
    return time.strftime('%b %d, %Y', d), time.strftime('%I:%M %p', d).lstrip('0')


def size(n):
    return f'{n / 1048576:.1f} MB' if n > 1048576 else f'{max(1, round(n / 1024))} KB'


def clean_title(t):
    # 'Metal Gear Solid (USA) (Disc 1)' -> 'Metal Gear Solid', 'USA · Disc 1'
    tags = re.findall(r'\(([^)]*)\)|\[([^\]]*)\]', t)
    base = re.sub(r'\s*[\(\[][^\)\]]*[\)\]]', '', t).strip() or t
    return base, ' · '.join(a or b for a, b in tags)


VIA = {'adb': 'USB', 'wifi': 'Wi-Fi', 'mtp': 'File transfer'}


def short_name(d):
    if d['kind'] == 'android':
        return d['model']
    letter = d['saves'][:2]
    return ('Handheld' if d['kind'] == 'handheld' else 'RetroBat') + f' {letter}'


def free_space(d):
    if d['kind'] == 'android':
        return 0
    try:
        return shutil.disk_usage(d['saves'][:3]).free
    except Exception:
        return 0


def roms_root(d):
    return Path(d['saves']) if d['kind'] == 'handheld' else Path(d['saves']).parent / 'roms'


def cover_index(devs, systems):
    # box art paths from each device's gamelist.xml
    idx = {}
    for d in devs:
        root = roms_root(d)
        for sysname in systems:
            gl = root / sysname / 'gamelist.xml'
            if not gl.exists():
                continue
            try:
                games = ET.parse(gl).getroot().iter('game')
            except Exception:
                continue
            for g in games:
                img = next((g.findtext(t) for t in ('thumbnail', 'image') if g.findtext(t)), None)
                if not img:
                    continue
                p = (gl.parent / img).resolve()
                if not p.exists():
                    continue
                stem = Path(g.findtext('path') or '').stem
                for name in (stem, re.sub(r'\s*\([^)]*\)', '', stem), g.findtext('name') or ''):
                    k = engine.norm(name)
                    if k:
                        idx.setdefault((sysname, k), str(p))
    return idx


def xenia_icon(devs, tid):
    # XBLA stuff never has scraped art, but the .gpd has the dashboard tile baked in
    for d in devs:
        for gpd in Path(d['saves'], 'xbox360').glob(f'**/{tid}.gpd'):
            b = gpd.read_bytes()
            start = b.find(bytes([0x89]) + b'PNG')
            end = b.find(b'IEND', start)
            if start != -1 and end != -1:
                return io.BytesIO(b[start:end + 8])
    return None


def find_cover(idx, row, devs):
    base = clean_title(row['title'])[0]
    for k in (row['key'], engine.norm(row['title']), engine.norm(base)):
        if (row['system'], k) in idx:
            return idx[(row['system'], k)]
    if row['system'] == 'xbox360':
        return xenia_icon(devs, row['key'])
    return None


def group_of(r):
    if r['status'] == 'same':
        return 'same'
    return 'action' if (r['can_ab'] or r['can_ba']) else 'stuck'


GROUPS = {'action': ('NEEDS ATTENTION', AMBER), 'same': ('IN SYNC', GREEN),
          'stuck': ("CAN'T MOVE BETWEEN THESE TWO DEVICES", FAINT)}
ORDER = {'action': 0, 'same': 1, 'stuck': 2}


def rounded(img, r=6):
    mask = Image.new('L', img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], r, fill=255)
    img.putalpha(mask)
    return img


def cover_image(path):
    try:
        im = Image.open(path).convert('RGBA')
        im.thumbnail((COVER_W * 2, COVER_H * 2))
        canvas = Image.new('RGBA', (COVER_W, COVER_H), (20, 29, 45, 255))
        im.thumbnail((COVER_W, COVER_H), Image.LANCZOS)
        canvas.alpha_composite(im, ((COVER_W - im.width) // 2, (COVER_H - im.height) // 2))
        return rounded(canvas)
    except Exception:
        return None


def placeholder(system):
    im = Image.new('RGBA', (COVER_W, COVER_H), (26, 37, 56, 255))
    d = ImageDraw.Draw(im)
    d.rectangle([4, 4, COVER_W - 5, COVER_H - 5], outline=(51, 211, 198, 90))
    label = system.upper()[:8]
    d.text((COVER_W / 2, COVER_H / 2), label, fill=(141, 152, 171, 255), anchor='mm')
    return rounded(im)


class Pill(tk.Label):
    def __init__(self, master, text, color, **kw):
        super().__init__(master, text=text, fg=color, bg=SURFACE, font=(F_UI, 9, 'bold'),
                         padx=10, pady=3, highlightthickness=1, highlightbackground=color, **kw)


class FlatButton(tk.Label):
    def __init__(self, master, text, command, primary=False, font=None, padx=14, pady=7, **kw):
        self.primary, self.command, self.enabled = primary, command, True
        super().__init__(master, text=text, cursor='hand2', padx=padx, pady=pady,
                         font=font or (F_UI, 10, 'bold' if primary else 'normal'), **kw)
        self.paint(False)
        self.bind('<Enter>', lambda e: self.paint(True))
        self.bind('<Leave>', lambda e: self.paint(False))
        self.bind('<Button-1>', lambda e: self.enabled and self.command())

    def paint(self, hover):
        if not self.enabled:
            return self.configure(bg=self.master['bg'], fg=FAINT, cursor='arrow')
        if self.primary:
            self.configure(bg='#5ee6db' if hover else CYAN, fg='#06201e', cursor='hand2')
        else:
            self.configure(bg=CARD_HI if hover else SURFACE, fg=CYAN if hover else INK, cursor='hand2')

    def set_enabled(self, on):
        self.enabled = on
        self.paint(False)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Save Porter')
        self.geometry('1180x760')
        self.minsize(900, 520)
        self.configure(bg=BG)
        try:
            self.iconbitmap(str(engine.ASSETS / 'icon.ico'))
        except Exception:
            pass
        self.devices, self.rows, self.covers = [], [], {}
        self.a = self.b = None
        self.img_cache = {}
        self.filter = tk.StringVar(value='all')
        self.extra = tk.BooleanVar(value=False)
        self.q = tk.StringVar()
        self.build()
        self.protocol('WM_DELETE_WINDOW', self.quit_app)
        self.after(50, self.load_devices)

    def quit_app(self):
        self.destroy()
        engine.adb_shutdown()

    def build(self):
        head = tk.Frame(self, bg=BG, padx=24, pady=18)
        head.pack(fill='x')
        tk.Label(head, text='SAVE PORTER', bg=BG, fg=CYAN, font=(F_MONO, 20, 'bold')).pack(side='left')
        tk.Label(head, text='   copy saves between your devices, nothing is ever deleted',
                 bg=BG, fg=DIM, font=(F_UI, 10)).pack(side='left', pady=(6, 0))
        FlatButton(head, '↻  Rescan', self.load_devices, bg=SURFACE).pack(side='right')
        FlatButton(head, '📱  Connect phone', self.connect_phone, bg=SURFACE).pack(side='right', padx=(8, 0))
        FlatButton(head, '＋  Add folder', self.add_folder, bg=SURFACE).pack(side='right', padx=8)

        tiles = tk.Frame(self, bg=BG, padx=24)
        tiles.pack(fill='x')
        tiles.columnconfigure(0, weight=1, uniform='t')
        tiles.columnconfigure(2, weight=1, uniform='t')
        self.tileA = self.make_tile(tiles, 'A')
        self.tileA.grid(row=0, column=0, sticky='nsew')
        mid = tk.Frame(tiles, bg=BG, padx=14)
        mid.grid(row=0, column=1)
        FlatButton(mid, '⟷', self.swap, font=(F_UI, 16), padx=12, pady=4, bg=SURFACE).pack()
        tk.Label(mid, text='swap', bg=BG, fg=FAINT, font=(F_UI, 8)).pack()
        self.tileB = self.make_tile(tiles, 'B')
        self.tileB.grid(row=0, column=2, sticky='nsew')

        bar = tk.Frame(self, bg=BG, padx=24, pady=16)
        bar.pack(fill='x')
        search = tk.Frame(bar, bg=SURFACE, highlightthickness=1, highlightbackground=LINE)
        search.pack(side='left')
        tk.Label(search, text='⌕', bg=SURFACE, fg=DIM, font=(F_UI, 13)).pack(side='left', padx=(10, 2))
        e = tk.Entry(search, textvariable=self.q, bg=SURFACE, fg=INK, insertbackground=INK,
                     relief='flat', font=(F_UI, 11), width=28)
        e.pack(side='left', ipady=7, padx=(0, 10))
        self.q.trace_add('write', lambda *a: self.draw())
        self.chips = {}
        chipbox = tk.Frame(bar, bg=BG)
        chipbox.pack(side='left', padx=18)
        for key, text in (('all', 'All'), ('action', 'Needs attention'), ('same', 'In sync')):
            c = tk.Label(chipbox, text=text, font=(F_UI, 10), padx=12, pady=5, cursor='hand2')
            c.pack(side='left', padx=3)
            c.bind('<Button-1>', lambda e, k=key: (self.filter.set(k), self.draw()))
            self.chips[key] = c
        cb = tk.Checkbutton(bar, text='Show arcade & system data', variable=self.extra, command=self.draw,
                            bg=BG, fg=DIM, selectcolor=SURFACE, activebackground=BG,
                            activeforeground=INK, font=(F_UI, 9), bd=0, highlightthickness=0)
        cb.pack(side='right')

        # tk has no scrolling frame so it's a frame inside a canvas
        wrap = tk.Frame(self, bg=BG, padx=24)
        wrap.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(wrap, bg=BG, highlightthickness=0)
        sb = tk.Scrollbar(wrap, orient='vertical', command=self.canvas.yview, width=12)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.list = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window((0, 0), window=self.list, anchor='nw')
        self.list.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.bind_all('<MouseWheel>', lambda e: self.canvas.yview_scroll(int(-e.delta / 120) * 3, 'units'))

        self.status = tk.Label(self, text='', bg=BG, fg=DIM, font=(F_UI, 9), anchor='w', padx=24, pady=10)
        self.status.pack(fill='x')

    def make_tile(self, master, side):
        t = tk.Frame(master, bg=SURFACE, highlightthickness=1, highlightbackground=LINE,
                     padx=16, pady=12, cursor='hand2')
        top = tk.Frame(t, bg=SURFACE)
        top.pack(fill='x')
        t.tag = tk.Label(top, text=f'DEVICE {side}', bg=SURFACE, fg=AMBER, font=(F_MONO, 9, 'bold'))
        t.tag.pack(side='left')
        t.change = tk.Label(top, text='change ▾', bg=SURFACE, fg=FAINT, font=(F_UI, 9))
        t.change.pack(side='right')
        row = tk.Frame(t, bg=SURFACE)
        row.pack(fill='x', pady=(6, 0))
        t.icon = tk.Label(row, text='', bg=SURFACE, fg=CYAN, font=(F_UI, 22))
        t.icon.pack(side='left', padx=(0, 12))
        txt = tk.Frame(row, bg=SURFACE)
        txt.pack(side='left', fill='x')
        t.name = tk.Label(txt, text='', bg=SURFACE, fg=INK, font=(F_UI, 13, 'bold'), anchor='w')
        t.name.pack(anchor='w')
        t.sub = tk.Label(txt, text='', bg=SURFACE, fg=DIM, font=(F_UI, 9), anchor='w')
        t.sub.pack(anchor='w')
        t.side = side
        for w in (t, top, t.tag, t.change, row, t.icon, txt, t.name, t.sub):
            w.bind('<Button-1>', lambda e, tile=t: self.pick_device(tile))
            w.bind('<Enter>', lambda e, tile=t: tile.configure(highlightbackground=CYAN))
            w.bind('<Leave>', lambda e, tile=t: tile.configure(highlightbackground=LINE))
        return t

    def paint_tile(self, t, d):
        if not d:
            t.icon.configure(text='∅')
            t.name.configure(text='No device')
            t.sub.configure(text='plug something in, then Rescan')
            return
        if d['kind'] == 'android':
            apps = d.get('app_names')
            t.icon.configure(text='📱')
            t.name.configure(text=f"{d['model']} · Android")
            found = ', '.join(apps) if apps else ('no emulator saves found' if apps is not None else 'reading…')
            t.sub.configure(text=f"{VIA[d['via']]}   ·   {found}")
            return
        t.icon.configure(text='🎮' if d['kind'] == 'handheld' else '🖥')
        t.name.configure(text=('Handheld · ArkOS' if d['kind'] == 'handheld' else 'RetroBat'))
        t.sub.configure(text=f"{d['saves'][:2]}  {engine.drive_label(d['saves'][:3]) or ''}   ·   "
                             f"{free_space(d) / 1e9:.0f} GB free")

    def pick_device(self, tile):
        m = tk.Menu(self, tearoff=0, bg=SURFACE, fg=INK, activebackground=CARD_HI,
                    activeforeground=CYAN, font=(F_UI, 10), bd=0)
        for d in self.devices:
            m.add_command(label=f"   {d['name']}   ", command=lambda d=d: self.set_side(tile.side, d))
        m.tk_popup(tile.winfo_rootx(), tile.winfo_rooty() + tile.winfo_height())

    def set_side(self, side, d):
        if side == 'A':
            if self.b and self.b['id'] == d['id']:
                self.b = self.a
            self.a = d
        else:
            if self.a and self.a['id'] == d['id']:
                self.a = self.b
            self.b = d
        self.compare()

    def load_devices(self):
        self.message('Looking for devices…', '')

        def work():
            devs = sorted(engine.find_devices(), key=lambda d: d['kind'] != 'retrobat')
            self.after(0, lambda: self.devices_found(devs))
        threading.Thread(target=work, daemon=True).start()

    def devices_found(self, devs):
        self.devices = devs
        ids = [d['id'] for d in self.devices]
        if not self.a or self.a['id'] not in ids:
            self.a = self.devices[0] if self.devices else None
        if not self.b or self.b['id'] not in ids or self.b['id'] == self.a['id']:
            self.b = next((d for d in self.devices if d['id'] != self.a['id']), None)
        self.compare()

    def compare(self):
        self.paint_tile(self.tileA, self.a)
        self.paint_tile(self.tileB, self.b)
        self.rows = []
        if not (self.a and self.b):
            return self.message('Only one device found.',
                                'Plug in your other drive or the handheld SD card, then hit Rescan.')
        self.message('Reading saves…', '')
        a, b = self.a, self.b

        def work():
            try:
                rows = engine.compare(a, b)
                idx = cover_index([a, b], {r['system'] for r in rows if not r['extra']})
                for r in rows:
                    r['cover'] = find_cover(idx, r, [a, b])
                self.after(0, lambda: self.done(rows))
            except Exception as ex:
                err = str(ex)
                self.after(0, lambda: self.message('Something went wrong', err))
        threading.Thread(target=work, daemon=True).start()

    def done(self, rows):
        self.rows = rows
        self.paint_tile(self.tileA, self.a)  # phone tile can show its apps now
        self.paint_tile(self.tileB, self.b)
        self.draw()

    def message(self, title, sub):
        for w in self.list.winfo_children():
            w.destroy()
        box = tk.Frame(self.list, bg=BG, pady=80)
        box.pack(fill='x')
        tk.Label(box, text=title, bg=BG, fg=INK, font=(F_UI, 14, 'bold')).pack()
        tk.Label(box, text=sub, bg=BG, fg=DIM, font=(F_UI, 10)).pack(pady=4)
        self.status.configure(text='')

    def visible(self):
        q, f, extra = self.q.get().lower(), self.filter.get(), self.extra.get()
        out = []
        for r in self.rows:
            if r['extra'] and not extra:
                continue
            if q and q not in r['title'].lower() and q not in r['system']:
                continue
            if f == 'action' and group_of(r) != 'action':
                continue
            if f == 'same' and r['status'] != 'same':
                continue
            out.append(r)
        return sorted(out, key=lambda r: (ORDER[group_of(r)], r['system'], r['title'].lower()))

    def draw(self):
        for key, c in self.chips.items():
            on = self.filter.get() == key
            c.configure(bg=CYAN if on else SURFACE, fg='#06201e' if on else DIM)
        if not self.rows:
            return
        for w in self.list.winfo_children():
            w.destroy()
        vis = self.visible()
        if not vis:
            return self.message('Nothing here', 'Try a different filter or search.')
        counts = {}
        for r in vis:
            counts[group_of(r)] = counts.get(group_of(r), 0) + 1
        need = counts.get('action', 0)
        shown = vis[:MAX_CARDS]
        last_group = None
        for r in shown:
            group = group_of(r)
            if group != last_group:
                text, color = GROUPS[group]
                tk.Label(self.list, text=f'{text}  ·  {counts[group]}', bg=BG, fg=color,
                         font=(F_MONO, 9, 'bold'), anchor='w', pady=8).pack(fill='x', pady=(10, 0))
                last_group = group
            self.card(r)
        self.canvas.yview_moveto(0)
        more = f'  ·  showing first {MAX_CARDS}, search to narrow down' if len(vis) > MAX_CARDS else ''
        self.status.configure(text=f'{len(vis)} games  ·  {need} need attention{more}', fg=DIM)

    def cover_for(self, r):
        key = (r['system'], r['key']) if isinstance(r['cover'], io.BytesIO) else (r['cover'] or ('ph', r['system']))
        if key not in self.img_cache:
            im = cover_image(r['cover']) if r['cover'] else None
            self.img_cache[key] = ImageTk.PhotoImage(im or placeholder(r['system']))
        return self.img_cache[key]

    def card(self, r):
        stuck = group_of(r) == 'stuck'
        same = r['status'] == 'same' or stuck
        bg = CARD
        c = tk.Frame(self.list, bg=bg, highlightthickness=1, highlightbackground=LINE, padx=12, pady=10)
        c.pack(fill='x', pady=4)
        c.columnconfigure(1, weight=1)

        tk.Label(c, image=self.cover_for(r), bg=bg).grid(row=0, column=0, rowspan=2, padx=(0, 14))
        title, tags = clean_title(r['title'])
        tk.Label(c, text=title, bg=bg, fg=DIM if same else INK, font=(F_UI, 12, 'bold'),
                 anchor='w').grid(row=0, column=1, sticky='sw')
        meta = CONSOLES.get(r['system'], r['system'].upper()) + (f'   ·   {tags}' if tags else '')
        tk.Label(c, text=meta, bg=bg, fg=FAINT if same else DIM, font=(F_UI, 9),
                 anchor='w').grid(row=1, column=1, sticky='nw')

        a_new, b_new = (r['status'] == 'a_newer') and not stuck, (r['status'] == 'b_newer') and not stuck
        self.slot(c, r['a'], a_new, same).grid(row=0, column=2, rowspan=2, padx=10)

        mid = tk.Frame(c, bg=bg, width=170)
        mid.grid(row=0, column=3, rowspan=2, padx=6)
        label, color = self.status_label(r)
        if stuck:
            color = FAINT
        Pill(mid, label, color).pack()
        if stuck:
            why = ("console unknown, game isn't on your PC" if r['system'] == 'retroarch'
                   else 'this save format stays put')
            tk.Label(mid, text=why, bg=bg, fg=FAINT, font=(F_UI, 8)).pack(pady=(6, 0))
            self.slot(c, r['b'], False, True).grid(row=0, column=4, rowspan=2, padx=10)
            return
        btns = tk.Frame(mid, bg=bg)
        btns.pack(pady=(8, 0))
        ab_ok = r['can_ab'] and not same
        ba_ok = r['can_ba'] and not same
        left = FlatButton(btns, '◀  to A', lambda r=r: self.send(r, 'ba'), primary=b_new or r['status'] == 'only_b',
                          font=(F_UI, 9, 'bold'), padx=10, pady=4, bg=bg)
        right = FlatButton(btns, 'to B  ▶', lambda r=r: self.send(r, 'ab'), primary=a_new or r['status'] == 'only_a',
                           font=(F_UI, 9, 'bold'), padx=10, pady=4, bg=bg)
        left.set_enabled(ba_ok)
        right.set_enabled(ab_ok)
        left.pack(side='left', padx=3)
        right.pack(side='left', padx=3)

        self.slot(c, r['b'], b_new, same).grid(row=0, column=4, rowspan=2, padx=10)

    def slot(self, master, s, newer, same):
        f = tk.Frame(master, bg=SURFACE if s else master['bg'], width=180, height=64,
                     highlightthickness=1, highlightbackground=(AMBER if newer else LINE) if s else LINE)
        f.pack_propagate(False)
        if not s:
            tk.Label(f, text='no save', bg=master['bg'], fg=FAINT, font=(F_UI, 9, 'italic')).pack(expand=True)
            return f
        day, clock = when(s['mtime'])
        col = FAINT if same else (AMBER if newer else INK)
        tk.Label(f, text=day, bg=SURFACE, fg=col, font=(F_UI, 10, 'bold')).pack(pady=(8, 0))
        extra = f"   ·   {s['n']} files" if s['n'] > 1 else ''
        tk.Label(f, text=f'{clock}   ·   {size(s["size"])}{extra}', bg=SURFACE,
                 fg=FAINT if same else DIM, font=(F_UI, 8)).pack()
        return f

    def status_label(self, r):
        A, B = short_name(self.a), short_name(self.b)
        return {'same': ('✓  In sync', GREEN), 'a_newer': (f'{A} is newer', AMBER),
                'b_newer': (f'{B} is newer', AMBER), 'only_a': (f'Only on {A}', CORAL),
                'only_b': (f'Only on {B}', CORAL)}[r['status']]

    def send(self, r, way):
        src, dst = (self.a, self.b) if way == 'ab' else (self.b, self.a)
        name = clean_title(r['title'])[0]
        self.status.configure(text=f'Copying {name} to {short_name(dst)}…', fg=AMBER)

        def work():  # phones are slow, don't freeze the window
            try:
                res = engine.transfer(src, dst, r['system'], r['key'])
            except Exception as ex:
                err = f"{r['title']}\n\n{ex}"
                self.after(0, lambda: (self.status.configure(text='', fg=DIM),
                                       messagebox.showerror('Save Porter', err)))
                return
            msg = f'✓  {name} copied to {short_name(dst)}'
            if res['backed_up']:
                msg += '  ·  the old save there was backed up'
            self.after(0, self.compare)
            self.after(900, lambda: self.status.configure(text=msg, fg=GREEN))
            self.after(7000, lambda: self.status.configure(fg=DIM))
        threading.Thread(target=work, daemon=True).start()

    def swap(self):
        self.a, self.b = self.b, self.a
        self.compare()

    def connect_phone(self):
        ConnectDialog(self)

    def add_folder(self):
        p = filedialog.askdirectory(title='Pick a RetroBat folder')
        if not p:
            return
        p = p.replace('/', '\\')
        if not engine.detect_retrobat(p):
            return messagebox.showerror('Save Porter', 'No RetroBat install found in that folder.')
        engine.add_extra(p)
        self.load_devices()


class ConnectDialog(tk.Toplevel):

    STEPS = [
        ('USB, file transfer', 'Plug in, unlock the phone, tap the USB notification and pick '
                               '"File transfer". Then hit Rescan. Nothing to set up.'),
        ('USB, ADB (faster)', 'Settings > About phone > tap Build number 7 times. Then Developer '
                              'options > turn on USB debugging. Plug in, allow the prompt, Rescan.'),
        ('Wi-Fi, no cable', 'Developer options > turn on Wireless debugging. Enter the IP address '
                            'and port it shows below. First time only: tap "Pair device with pairing '
                            'code" and fill in the two pairing fields as well.'),
    ]

    def __init__(self, app):
        super().__init__(app, bg=BG, padx=24, pady=20)
        self.app = app
        self.title('Connect a phone')
        try:
            self.iconbitmap(str(engine.ASSETS / 'icon.ico'))
        except Exception:
            pass
        self.resizable(False, False)
        self.transient(app)
        tk.Label(self, text='CONNECT AN ANDROID PHONE', bg=BG, fg=CYAN,
                 font=(F_MONO, 12, 'bold')).pack(anchor='w')
        for head, body in self.STEPS:
            tk.Label(self, text=head, bg=BG, fg=AMBER, font=(F_UI, 10, 'bold')).pack(anchor='w', pady=(14, 0))
            tk.Label(self, text=body, bg=BG, fg=DIM, font=(F_UI, 9), wraplength=460,
                     justify='left').pack(anchor='w')
        form = tk.Frame(self, bg=BG)
        form.pack(fill='x', pady=(14, 0))
        self.fields = {}
        for row, (key, label) in enumerate((('addr', 'IP address : port'), ('code', 'Pairing code'),
                                            ('paddr', 'Pairing IP : port'))):
            tk.Label(form, text=label, bg=BG, fg=INK, font=(F_UI, 9)).grid(row=row, column=0, sticky='w', pady=3)
            e = tk.Entry(form, bg=SURFACE, fg=INK, insertbackground=INK, relief='flat', font=(F_UI, 10), width=26)
            e.grid(row=row, column=1, sticky='w', padx=(12, 0), ipady=4, pady=3)
            self.fields[key] = e
        tk.Label(form, text='first time only', bg=BG, fg=FAINT, font=(F_UI, 8)).grid(row=1, column=2, padx=8)
        has_adb = bool(engine.adb_path())
        self.note = tk.Label(self, text='' if has_adb else
                             'ADB is not installed, so only file transfer mode works for now.',
                             bg=BG, fg=DIM if has_adb else CORAL, font=(F_UI, 9), wraplength=460,
                             justify='left')
        self.note.pack(anchor='w', pady=(10, 0))
        btns = tk.Frame(self, bg=BG)
        btns.pack(fill='x', pady=(14, 0))
        go = FlatButton(btns, 'Connect over Wi-Fi', self.connect, primary=True, bg=BG)
        go.set_enabled(has_adb)
        go.pack(side='right')
        FlatButton(btns, 'Close', self.destroy, bg=SURFACE).pack(side='right', padx=8)

    def connect(self):
        addr = self.fields['addr'].get().strip()
        code = self.fields['code'].get().strip() or None
        paddr = self.fields['paddr'].get().strip() or None
        if not addr:
            return self.note.configure(text='Enter the IP address and port first.', fg=CORAL)
        self.note.configure(text='Connecting…', fg=AMBER)

        def work():
            try:
                engine.adb_connect(addr, code, paddr)
            except Exception as ex:
                err = str(ex)
                self.after(0, lambda: self.note.configure(text=err, fg=CORAL))
                return
            self.after(0, lambda: (self.destroy(), self.app.load_devices()))
        threading.Thread(target=work, daemon=True).start()


if __name__ == '__main__':
    App().mainloop()
