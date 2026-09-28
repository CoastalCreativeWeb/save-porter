# Save Porter - finds emulator saves on whatever's plugged in and copies them around.
#
# Each save file carries two paths:
#   rel   = where it actually is on that device
#   canon = where RetroBat would put it (saves/<system>/...)
# canon is what lets a PC save, an R36S save and a phone save line up as the same game.
import json, os, re, shutil, string, struct, subprocess, sys, tempfile, time
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).parent
# when frozen, bundled stuff lives in _MEIPASS but config should sit next to the exe
ASSETS = Path(getattr(sys, '_MEIPASS', HERE))
if getattr(sys, 'frozen', False):
    HERE = Path(sys.executable).parent
BACKUP_DIR = '_saveporter_backups'
CONFIG = HERE / 'devices.json'   # folders added by hand
LEDGER = HERE / 'ledger.json'    # see ledger_fix()
NO_WINDOW = 0x08000000           # stops adb/powershell flashing a console window

SKIP_DIRS = {BACKUP_DIR, 'crash_report', 'ResourcePacks', 'logs', 'cache',
             'shadercache', 'shaders', 'drm', 'disc', 'game', 'Cache'}
ARCADE = {'mame', 'hbmame', 'naomi', 'naomi2', 'atomiswave', 'fbneo', 'cps1',
          'cps2', 'cps3', 'neogeo'}
# not really game saves, just emulator junk. hidden by default
SYSTEM_DATA = {'dolphin', 'xbox', 'switch', 'psvita'}
NICE = {'mcd001': 'Memory Card 1', 'mcd002': 'Memory Card 2'}
HANDHELD_EXT = {'.srm', '.sav', '.mcr', '.mcd', '.eep', '.fla', '.sra', '.mpk',
                '.rtc', '.dsv'}
ROM_EXT = {'.chd', '.cue', '.bin', '.pbp', '.iso', '.m3u', '.img'}

# (app, folder on the phone, layout)
ANDROID_APPS = [
    ('RetroArch', 'RetroArch/saves', 'ra'),
    ('RetroArch', 'Android/data/com.retroarch/files/saves', 'ra'),
    ('RetroArch', 'Android/data/com.retroarch.aarch64/files/saves', 'ra'),
    ('RetroArch', 'Android/media/com.retroarch/RetroArch/saves', 'ra'),
    ('Lemuroid', 'Android/data/com.swordfish.lemuroid/files/saves', 'ra'),
    ('DuckStation', 'Android/data/com.github.stenzek.duckstation/files/memcards', 'duck'),
    ('NetherSX2', 'Android/data/xyz.aethersx2.android/files/memcards', 'pcsx2'),
    ('Dolphin', 'Android/data/org.dolphinemu.dolphinemu/files/GC', 'dolphin'),
    ('Dolphin', 'dolphin-emu/GC', 'dolphin'),
    ('PPSSPP', 'PSP/SAVEDATA', 'ppsspp'),
]
ANDROID_ROOT = '/storage/emulated/0'
RA_PACKAGES = ['com.retroarch', 'com.retroarch.aarch64', 'com.retroarch.ra32']
# where a brand new save goes when RetroArch sorts saves by core and the phone
# has nothing for that console yet
DEFAULT_CORE_DIR = {'gba': 'mGBA', 'snes': 'Snes9x', 'nes': 'FCEUmm', 'gb': 'Gambatte',
                    'gbc': 'Gambatte', 'megadrive': 'Genesis Plus GX', 'psx': 'SwanStation',
                    'n64': 'Mupen64Plus-Next', 'nds': 'melonDS'}
# for people who turned on "sort saves by core" in RetroArch
CORE_SYSTEMS = {'snes9x': 'snes', 'bsnes': 'snes', 'mgba': 'gba', 'vba': 'gba', 'gambatte': 'gbc',
                'sameboy': 'gbc', 'mesen': 'nes', 'fceumm': 'nes', 'nestopia': 'nes',
                'genesis plus gx': 'megadrive', 'picodrive': 'megadrive', 'mupen64plus': 'n64',
                'parallel n64': 'n64', 'swanstation': 'psx', 'pcsx-rearmed': 'psx',
                'beetle psx': 'psx', 'melonds': 'nds', 'desmume': 'nds'}


class LocalFS:
    lossy_times = False

    def __init__(self, root):
        self.root = Path(root)

    def read(self, rel):
        return (self.root / rel).read_bytes()

    def write(self, rel, data, mtime):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        os.utime(p, (mtime, mtime))


def adb_path():
    for p in (HERE / 'platform-tools' / 'adb.exe', HERE / 'adb.exe', ASSETS / 'adb.exe'):
        if p.exists():
            return str(p)
    return shutil.which('adb')


def run(args, data=None, timeout=60):
    return subprocess.run(args, input=data, capture_output=True, timeout=timeout,
                          creationflags=NO_WINDOW)


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


class AdbFS:
    lossy_times = True  # touch usually works but not on every phone

    def __init__(self, serial):
        self.serial, self.adb = serial, adb_path()

    def shell(self, cmd, timeout=60):
        r = run([self.adb, '-s', self.serial, 'shell', cmd], timeout=timeout)
        return r.stdout.decode('utf-8', 'replace')

    def list_many(self, bases):
        # one shell call for everything, adb round trips are slow
        script = ';'.join(
            f"if [ -d {sh_quote(ANDROID_ROOT + '/' + b)} ]; then echo {sh_quote('D|' + b)}; "
            f"find {sh_quote(ANDROID_ROOT + '/' + b)} -type f -exec stat -c '%s|%Y|%n' {{}} + 2>/dev/null; fi"
            for b in bases)
        out, cur = {}, None
        for line in self.shell(script).splitlines():
            if line.startswith('D|'):
                cur = line[2:]
                out[cur] = []
            elif cur and line.count('|') >= 2:
                size, mtime, path = line.split('|', 2)
                out[cur].append((path[len(ANDROID_ROOT) + 1:], int(size), float(mtime)))
        return out

    def read(self, rel):
        r = run([self.adb, '-s', self.serial, 'exec-out', 'cat ' + sh_quote(f'{ANDROID_ROOT}/{rel}')])
        if r.returncode:
            raise OSError(f'Could not read {rel} from the phone')
        return r.stdout

    def write(self, rel, data, mtime):
        remote = f'{ANDROID_ROOT}/{rel}'
        self.shell(f'mkdir -p {sh_quote(remote.rsplit("/", 1)[0])}')
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp, 'save')
            local.write_bytes(data)
            r = run([self.adb, '-s', self.serial, 'push', str(local), remote], timeout=120)
            if r.returncode:
                raise OSError(r.stderr.decode(errors='replace').strip() or 'adb push failed')
        stamp = time.strftime('%Y%m%d%H%M.%S', time.localtime(mtime))
        self.shell(f'touch -m -t {stamp} {sh_quote(remote)}')


MTP_SCRIPT = r'''
param($op, $dev, $arg1, $arg2)
$ErrorActionPreference = 'Stop'
$sh = New-Object -ComObject Shell.Application
$phone = $sh.NameSpace(17).Items() | Where-Object { $_.Name -eq $dev } | Select-Object -First 1
if (-not $phone) { throw "Phone not connected" }
$storage = $phone.GetFolder.Items() | Select-Object -First 1
if (-not $storage) { throw "Can't see the phone's storage. Unlock it and set USB to File transfer" }
function FName($i) { $n = $i.ExtendedProperty('System.FileName'); if ($n) { $n } else { $i.Name } }
function Child($folder, $name) {
  # ParseName is instant, the loop is only a fallback for phones that don't support it
  $it = $folder.ParseName($name)
  if ($it) { return $it }
  $folder.Items() | Where-Object { (FName $_) -eq $name } | Select-Object -First 1
}
# Android/data has hundreds of entries, so remember folders we've already walked into
$seen = @{ '' = $storage.GetFolder }
function Nav($rel, [switch]$create) {
  $path = ''
  $f = $seen['']
  foreach ($seg in ($rel -split '/' | Where-Object { $_ })) {
    $path = "$path/$seg"
    if ($seen.ContainsKey($path)) { $f = $seen[$path]; continue }
    $it = Child $f $seg
    if (-not $it) {
      if (-not $create) { return $null }
      $f.NewFolder($seg); Start-Sleep -Milliseconds 400; $it = Child $f $seg
    }
    $f = $it.GetFolder
    $seen[$path] = $f
  }
  $f
}
function Walk($f, $prefix) {
  foreach ($i in $f.Items()) {
    $n = FName $i
    if ($i.IsFolder) { Walk $i.GetFolder "$prefix/$n" }
    else {
      # ModifyDate comes back as 1899 on some phones, System.DateModified (UTC) is reliable
      $d = $i.ExtendedProperty('System.DateModified')
      if ($d) { $t = [DateTimeOffset]::new([DateTime]::SpecifyKind($d, 'Utc')).ToUnixTimeSeconds() }
      else { $t = [DateTimeOffset]::new($i.ModifyDate).ToUnixTimeSeconds() }
      "F|$prefix/$n|$($i.ExtendedProperty('System.Size'))|$t"
    }
  }
}
function WaitFile($path) {
  # the size the phone reports can be stale, so just wait for the copy to stop growing
  $last = -1
  for ($k = 0; $k -lt 300; $k++) {
    if (Test-Path -LiteralPath $path) {
      $len = (Get-Item -LiteralPath $path).Length
      if ($len -gt 0 -and $len -eq $last) { return }
      $last = $len
    } elseif ($k -gt 66) {
      throw "The phone stopped responding. Unplug it, plug it back in and pick File transfer"
    }
    Start-Sleep -Milliseconds 150
  }
  throw "Timed out copying $path"
}
switch ($op) {
  'list' {
    foreach ($b in ($arg1 -split '\|')) {
      $f = Nav $b
      if ($f) { "D|$b"; Walk $f $b }
    }
  }
  'read' {
    $parts = $arg1 -split '/'
    $item = Child (Nav ($parts[0..($parts.Count - 2)] -join '/')) $parts[-1]
    $sh.NameSpace($arg2).CopyHere($item, 4 + 16 + 1024)
    WaitFile (Join-Path $arg2 $parts[-1])
  }
  'write' {
    $parts = $arg1 -split '/'
    $f = Nav ($parts[0..($parts.Count - 2)] -join '/') -create
    $f.CopyHere((Join-Path $arg2 $parts[-1]), 4 + 16 + 512 + 1024)
    for ($k = 0; $k -lt 300; $k++) { if (Child $f $parts[-1]) { break }; Start-Sleep -Milliseconds 100 }
  }
}
'''


class MtpFS:
    # plain "File transfer" mode. no dev mode needed, but it's slow and MTP
    # can't set modified times so we lean on the ledger
    lossy_times = True

    def __init__(self, name):
        self.name = name
        self.script = Path(tempfile.gettempdir(), 'saveporter_mtp.ps1')
        self.script.write_text(MTP_SCRIPT, encoding='utf-8')

    def ps(self, op, a1='', a2='', timeout=180):
        r = run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.script),
                 op, self.name, a1, a2], timeout=timeout)
        if r.returncode:
            lines = r.stderr.decode(errors='replace').strip().splitlines()
            raise OSError(lines[0] if lines else 'Phone transfer failed')
        return r.stdout.decode('utf-8', 'replace')

    def list_many(self, bases):
        out, cur = {}, None
        for line in self.ps('list', '|'.join(bases)).splitlines():
            if line.startswith('D|'):
                cur = line[2:]
                out[cur] = []
            elif line.startswith('F|') and cur:
                _, rel, size, mtime = line.split('|')
                out[cur].append((rel, int(size or 0), float(mtime)))
        return out

    def read(self, rel):
        with tempfile.TemporaryDirectory() as tmp:
            self.ps('read', rel, tmp)
            return Path(tmp, rel.rsplit('/', 1)[-1]).read_bytes()

    def write(self, rel, data, mtime):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, rel.rsplit('/', 1)[-1]).write_bytes(data)
            self.ps('write', rel, tmp)


def fs_for(dev):
    if dev['kind'] == 'android':
        return MtpFS(dev['serial']) if dev['via'] == 'mtp' else AdbFS(dev['serial'])
    return LocalFS(dev['saves'])


def ledger():
    try:
        return json.loads(LEDGER.read_text())
    except Exception:
        return {}


def ledger_note(dev, rel, size, real_mtime, device_mtime):
    lg = ledger()
    lg[f"{dev['id']}|{rel}"] = [size, real_mtime, device_mtime]
    LEDGER.write_text(json.dumps(lg))


def ledger_fix(dev, rel, size, mtime, lg):
    # MTP stamps copied files with "now", so a copy would look newer than its source.
    # if we wrote it and it hasn't been touched since, use the original time instead
    e = lg.get(f"{dev['id']}|{rel}")
    if e and e[0] == size and abs(e[2] - mtime) < 5:
        return e[1]
    return mtime


def drive_label(root):
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        ctypes.windll.kernel32.GetVolumeInformationW(root, buf, 261, None, None, None, None, 0)
        return buf.value
    except Exception:
        return ''


def detect_retrobat(folder):
    folder = Path(folder)
    if (folder / 'saves').is_dir() and ((folder / 'retrobat.exe').exists()
                                        or (folder / 'emulationstation').is_dir()):
        return folder / 'saves'
    return None


def adb_devices():
    adb = adb_path()
    if not adb:
        return []
    try:
        out = run([adb, 'devices', '-l'], timeout=15).stdout.decode(errors='replace')
    except Exception:
        return []
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[1] != 'device':
            continue
        serial = parts[0]
        model = next((p[6:] for p in parts if p.startswith('model:')), 'Android').replace('_', ' ')
        wifi = ':' in serial or serial.startswith('adb-')
        devs.append(dict(id=f'adb:{serial}', kind='android', via='wifi' if wifi else 'adb',
                         serial=serial, saves=model, model=model,
                         name=f"{model} (Android, {'Wi-Fi' if wifi else 'USB'})"))
    return devs


def mtp_devices():
    # phones in file transfer mode show up under This PC with no drive letter
    try:
        r = run(['powershell', '-NoProfile', '-Command',
                 "(New-Object -ComObject Shell.Application).NameSpace(17).Items() | "
                 "Where-Object { -not $_.IsFileSystem } | ForEach-Object { $_.Name }"], timeout=20)
    except Exception:
        return []
    return [dict(id=f'mtp:{n}', kind='android', via='mtp', serial=n, saves=n, model=n,
                 name=f'{n} (Android, file transfer)')
            for n in (x.strip() for x in r.stdout.decode(errors='replace').splitlines()) if n]


def adb_shutdown():
    # adb leaves a server running in the background otherwise
    adb = adb_path()
    if adb:
        try:
            run([adb, 'kill-server'], timeout=10)
        except Exception:
            pass


def adb_connect(address, pair_code=None, pair_address=None):
    # android 11+ wants a one time pair before connect works
    adb = adb_path()
    if not adb:
        raise OSError('ADB is not installed')
    if pair_code:
        r = run([adb, 'pair', pair_address or address, pair_code], timeout=30)
        text = (r.stdout + r.stderr).decode(errors='replace')
        if 'Successfully' not in text:
            raise OSError(text.strip() or 'Pairing failed')
    r = run([adb, 'connect', address], timeout=30)
    text = (r.stdout + r.stderr).decode(errors='replace').strip()
    if 'connected' not in text or 'cannot' in text or 'failed' in text:
        raise OSError(text or 'Could not connect')
    return text


def find_devices():
    devs = []
    for letter in string.ascii_uppercase:
        root = f'{letter}:\\'
        if not os.path.exists(root):
            continue
        label = drive_label(root)
        for cand in (root, root + 'Emulation', root + 'RetroBat', root + 'Retrobat'):
            saves = detect_retrobat(cand)
            if saves:
                devs.append(dict(id=str(saves), kind='retrobat', saves=str(saves),
                                 name=f'{letter}: {label or "Drive"} (RetroBat)'))
                break
        # R36S / ArkOS card, saves sit right next to the roms
        if label.upper() == 'EASYROMS' or (Path(root, 'psx').is_dir() and Path(root, 'snes').is_dir()
                                           and not Path(root, 'Emulation').exists()):
            devs.append(dict(id=root, kind='handheld', saves=root,
                             name=f'{letter}: {label or "Drive"} (Handheld, ArkOS)'))
    for extra in load_extra():
        saves = detect_retrobat(extra)
        if saves and not any(d['id'] == str(saves) for d in devs):
            devs.append(dict(id=str(saves), kind='retrobat', saves=str(saves),
                             name=f'{extra} (RetroBat)'))
    # a phone with usb debugging shows up in both lists, prefer adb
    devs += adb_devices() or mtp_devices()
    return devs


def load_extra():
    try:
        return json.loads(CONFIG.read_text())
    except Exception:
        return []


def add_extra(path):
    lst = load_extra()
    if path not in lst:
        lst.append(path)
        CONFIG.write_text(json.dumps(lst, indent=2))


def norm(s):
    s = re.sub(r'_\d+$', '', s)
    return re.sub(r'[^a-z0-9]', '', s.lower())


def pretty(stem):
    if stem.lower() in NICE:
        return NICE[stem.lower()]
    stem = re.sub(r'_\d+$', '', stem)
    stem = re.sub(r'^\d\d-[A-Z0-9]{4}-', '', stem)  # strip the 01-GWRE- bit off gamecube saves
    return stem.strip()


def sfo_title(b):
    # pulls TITLE out of a PARAM.SFO (psp/ps3/vita saves)
    try:
        if b[:4] != b'\x00PSF':
            return None
        kt, dt, n = struct.unpack_from('<III', b, 8)
        for i in range(n):
            ko, _fmt, ln, _mx, do = struct.unpack_from('<HHIII', b, 20 + i * 16)
            key = b[kt + ko:b.index(b'\x00', kt + ko)].decode()
            if key == 'TITLE':
                t = b[dt + do:dt + do + ln].rstrip(b'\x00').decode('utf-8', 'ignore')
                return re.sub(r'[\x00-\x1f�]', '', t).strip()
    except Exception:
        pass
    return None


def xenia_titles(root):
    # xenia saves are just title ids. the profile's FFFE07D1.gpd lists every game played:
    # big endian title id, then the name as UTF-16BE 40 bytes later
    names = {}
    for gpd in Path(root, 'xbox360').glob('**/FFFE07D1.gpd'):
        try:
            b = gpd.read_bytes()
        except OSError:
            continue
        for tid_file in gpd.parent.glob('*.gpd'):
            tid = tid_file.stem.upper()
            if tid in names or tid == 'FFFE07D1':
                continue
            try:
                raw = bytes.fromhex(tid)
            except ValueError:
                continue
            i = b.find(raw)
            while i != -1:
                name = b[i + 40:i + 300].decode('utf-16-be', 'ignore').split(chr(0))[0].strip()
                if len(name) >= 2 and name.isprintable():
                    names[tid] = name
                    break
                i = b.find(raw, i + 1)
    return names


@lru_cache(maxsize=1)
def rom_index():
    # RetroArch on android dumps every save in one folder, so we work out the
    # console by finding the matching rom on one of the drives
    idx = {}
    for letter in string.ascii_uppercase:
        root = Path(f'{letter}:\\')
        if not root.exists():
            continue
        for rom_root in (root / 'Emulation' / 'roms', root / 'RetroBat' / 'roms', root / 'roms', root):
            try:
                systems = [p for p in rom_root.iterdir() if p.is_dir()]
            except OSError:
                continue
            for sysdir in systems:
                try:
                    for f in sysdir.iterdir():
                        if f.is_file() and f.suffix.lower() not in {'.xml', '.txt', '.srm', '.png', '.jpg'}:
                            idx.setdefault(norm(f.stem), sysdir.name)
                except OSError:
                    pass
    return idx


def unit_key(parts, kind):
    if len(parts) < 2 or any(p in SKIP_DIRS for p in parts[:-1]):
        return None
    sysname, stem = parts[0], Path(parts[-1]).stem
    if kind == 'handheld':
        if len(parts) != 2 or Path(parts[-1]).suffix.lower() not in HANDHELD_EXT:
            return None
        return sysname, norm(stem), pretty(stem)
    lower = [p.lower() for p in parts]
    if 'savedata' in lower:  # psp/ps3/vita, one folder per save
        i = lower.index('savedata')
        if len(parts) <= i + 2:
            return None
        return sysname, parts[i + 1], parts[i + 1]
    if sysname == 'ps3':
        return None
    if 'content' in parts and sysname == 'xbox360':  # content/<profile>/<titleid>/...
        i = parts.index('content')
        if len(parts) <= i + 3 or parts[i + 2] == 'FFFE07D1':
            return None
        return sysname, parts[i + 2], f'Title {parts[i + 2]}'
    if sysname in ARCADE and len(parts) >= 4:  # mame/nvram/<game>/...
        return sysname, parts[2], parts[2]
    if Path(parts[-1]).suffix.lower() in {'.ini', '.log', '.txt', '.png', '.jpg'}:
        return None
    return sysname, norm(stem), pretty(stem)


def android_canon(base, layout, rel):
    sub = rel[len(base) + 1:].split('/')
    name = sub[-1]
    if layout == 'ra':
        if Path(name).suffix.lower() not in HANDHELD_EXT:
            return None
        system = rom_index().get(norm(Path(name).stem))
        if not system and len(sub) > 1:
            system = next((s for c, s in CORE_SYSTEMS.items() if c in sub[0].lower()), None)
        return f'{system or "retroarch"}/{name}'
    if layout == 'duck' and len(sub) == 1 and name.endswith('.mcd'):
        return f'psx/duckstation/memcards/{name}'
    if layout == 'pcsx2' and len(sub) == 1 and name.lower().endswith('.ps2'):
        return f'ps2/pcsx2/memcards/{name}'
    if layout == 'dolphin' and len(sub) == 3 and sub[1] == 'Card A' and name.endswith('.gci'):
        return f'gamecube/dolphin-emu/User/GC/{sub[0]}/{name}'
    if layout == 'ppsspp' and len(sub) >= 2:
        return 'psp/SAVEDATA/' + '/'.join(sub)
    return None


def android_rel(dev, canon):
    apps = dev.get('apps', {})
    parts = canon.split('/')

    def base(layout):
        return next((b for b, l in apps.items() if l == layout), None)

    if canon.startswith('psx/duckstation/memcards/') and base('duck'):
        return f"{base('duck')}/{parts[-1]}"
    if canon.startswith('ps2/pcsx2/memcards/') and base('pcsx2'):
        return f"{base('pcsx2')}/{parts[-1]}"
    if canon.startswith('gamecube/dolphin-emu/User/GC/') and len(parts) == 7 and base('dolphin'):
        return f"{base('dolphin')}/{parts[5]}/Card A/{parts[6]}"
    if canon.startswith('psp/SAVEDATA/'):
        return 'PSP/SAVEDATA/' + '/'.join(parts[2:])
    if (len(parts) == 2 and parts[0] != 'retroarch' and base('ra')
            and Path(parts[1]).suffix.lower() in HANDHELD_EXT):
        core = dev.get('ra_dirs', {}).get(parts[0]) or DEFAULT_CORE_DIR.get(parts[0])
        if dev.get('ra_sort') and core:
            return f"{base('ra')}/{core}/{parts[1]}"
        return f"{base('ra')}/{parts[1]}"
    return None


def to_rel(dev, canon):
    if dev['kind'] == 'retrobat':
        return None if canon.startswith('retroarch/') else canon
    if dev['kind'] == 'handheld':
        parts = canon.split('/')
        ok = len(parts) == 2 and parts[0] != 'retroarch' and Path(parts[1]).suffix.lower() in HANDHELD_EXT
        return canon if ok else None
    return android_rel(dev, canon)


def add_file(units, kind, rel, canon, size, mtime):
    k = unit_key(canon.split('/'), kind)
    if not k:
        return None
    sysname, key, title = k
    u = units.setdefault((sysname, key), dict(system=sysname, key=key, title=title,
                                              files=[], mtime=0, size=0))
    u['files'].append(dict(rel=rel, canon=canon, size=size, mtime=mtime))
    u['mtime'] = max(u['mtime'], mtime)
    u['size'] += size
    return u


def walk_local(root, kind, system=None):
    # on the handheld only look one level deep, no point crawling 50GB of roms
    try:
        top = [root / system] if system else [p for p in root.iterdir()
                                              if p.is_dir() and p.name not in SKIP_DIRS]
    except OSError:
        return
    for sysdir in top:
        if not sysdir.is_dir():
            continue
        if kind == 'handheld':
            try:
                yield from (p for p in sysdir.iterdir() if p.suffix.lower() in HANDHELD_EXT and p.is_file())
            except OSError:  # System Volume Information etc
                pass
            continue
        for dirpath, dirnames, files in os.walk(sysdir):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            yield from (Path(dirpath, f) for f in files)


def scan_local(dev, system=None):
    root = Path(dev['saves'])
    units = {}
    for full in walk_local(root, dev['kind'], system):
        rel = '/'.join(full.relative_to(root).parts)
        st = full.stat()
        u = add_file(units, dev['kind'], rel, rel, st.st_size, st.st_mtime)
        if u and full.name == 'PARAM.SFO':
            u['title'] = sfo_title(full.read_bytes()) or u['title']
    if any(k[0] == 'xbox360' for k in units):
        names = xenia_titles(root)
        for (sysname, key), u in units.items():
            if sysname == 'xbox360' and key.upper() in names:
                u['title'] = names[key.upper()]
    return units


_ra_cache = {}


def retroarch_config(dev, fs):
    # ask RetroArch itself where it saves instead of guessing. newer play store
    # builds use Android/media and often have "sort saves by core" on
    if dev['id'] not in _ra_cache:
        found = None
        for pkg in RA_PACKAGES:
            try:
                cfg = fs.read(f'Android/data/{pkg}/files/retroarch.cfg').decode('utf-8', 'replace')
            except Exception:
                continue
            opt = dict(re.findall(r'^(\w+) = "(.*)"$', cfg, re.M))
            folder = opt.get('savefile_directory', '')
            if folder.startswith(ANDROID_ROOT + '/'):
                found = (folder[len(ANDROID_ROOT) + 1:].rstrip('/'), opt.get('sort_savefiles_enable') == 'true')
                break
        _ra_cache[dev['id']] = found
    return _ra_cache[dev['id']]


def scan_android(dev):
    fs = fs_for(dev)
    layouts = {b: l for _, b, l in ANDROID_APPS}
    ra = retroarch_config(dev, fs)
    if ra:
        layouts = {ra[0]: 'ra', **layouts}
    listing = fs.list_many(list(layouts))
    # RetroArch's folder counts even before it exists, so a first save can be sent over
    dev['apps'] = {b: layouts[b] for b in layouts if b in listing or (ra and b == ra[0])}
    dev['ra_sort'] = bool(ra and ra[1])
    dev['ra_dirs'] = {}
    dev['app_names'] = sorted({a for a, b, _ in ANDROID_APPS if b in listing} | ({'RetroArch'} if ra else set()))
    lg, units = ledger(), {}
    for base, files in listing.items():
        for rel, size, mtime in files:
            canon = android_canon(base, layouts[base], rel)
            if not canon:
                continue
            sub = rel[len(base) + 1:].split('/')
            if layouts[base] == 'ra' and len(sub) > 1:
                dev['ra_dirs'].setdefault(canon.split('/')[0], sub[0])
            u = add_file(units, 'retrobat', rel, canon, size, ledger_fix(dev, rel, size, mtime, lg))
            if u and rel.endswith('PARAM.SFO'):
                try:
                    u['title'] = sfo_title(fs.read(rel)) or u['title']
                except OSError:
                    pass
    return units


def scan(dev, system=None):
    if dev['kind'] == 'android':
        units = scan_android(dev)
        return {k: u for k, u in units.items() if not system or k[0] == system}
    return scan_local(dev, system)


def is_card(f):
    # duckstation's slot 1 card or a retroarch .srm, same 128KB image either way
    c = f['canon']
    return c.endswith('_1.mcd') or (c.count('/') == 1 and c.endswith('.srm'))


def ps1_card(u):
    cards = [f for f in u['files'] if is_card(f)]
    return max(cards, key=lambda f: f['mtime']) if cards else None


def plan(u, src, dst, old):
    # returns [(src rel, dst rel)], or None if dst can't take this save
    if src['kind'] == dst['kind'] and src['kind'] != 'android':
        return [(f['rel'], f['rel']) for f in u['files']]
    if u['system'] == 'psx':
        card = ps1_card(u)
        if not card:
            return None
        if old and any(is_card(f) for f in old['files']):  # overwrite whatever copies are already there
            return [(card['rel'], f['rel']) for f in old['files'] if is_card(f)]
        if dst['kind'] == 'handheld':  # named after the rom so arkos finds it
            roms = [p.stem for p in Path(dst['saves'], 'psx').glob('*')
                    if p.suffix.lower() in ROM_EXT and norm(p.stem) == u['key']]
            return [(card['rel'], f"psx/{roms[0] if roms else u['title']}.srm")]
        name = card['canon'].rsplit('/', 1)[-1]
        mcd = name if name.endswith('.mcd') else f"{u['title']}_1.mcd"
        target = to_rel(dst, f'psx/duckstation/memcards/{mcd}') or to_rel(dst, f"psx/{Path(name).stem}.srm")
        return [(card['rel'], target)] if target else None
    old_by_name = {f['rel'].rsplit('/', 1)[-1]: f['rel'] for f in (old['files'] if old else [])}
    pairs = []
    for f in u['files']:
        d = old_by_name.get(f['rel'].rsplit('/', 1)[-1]) or to_rel(dst, f['canon'])
        if not d:
            return None
        pairs.append((f['rel'], d))
    return pairs


def compare(a_dev, b_dev):
    A, B = scan(a_dev), scan(b_dev)
    cross = a_dev['kind'] != b_dev['kind'] or a_dev['kind'] == 'android'
    rows = []
    for k in sorted(set(A) | set(B), key=lambda k: (k[0], (A.get(k) or B.get(k))['title'].lower())):
        a, b = A.get(k), B.get(k)
        if a and b:
            if cross and k[0] == 'psx':
                ca, cb = ps1_card(a), ps1_card(b)
                same = bool(ca and cb) and ca['size'] == cb['size'] and abs(ca['mtime'] - cb['mtime']) < 3
            else:
                sa = sorted((f['canon'], f['size']) for f in a['files'])
                sb = sorted((f['canon'], f['size']) for f in b['files'])
                same = sa == sb and abs(a['mtime'] - b['mtime']) < 3
            status = 'same' if same else ('a_newer' if a['mtime'] > b['mtime'] else 'b_newer')
        else:
            status = 'only_a' if a else 'only_b'
        rows.append(dict(
            system=k[0], key=k[1], title=(a or b)['title'], status=status,
            a=a and dict(mtime=a['mtime'], size=a['size'], n=len(a['files'])),
            b=b and dict(mtime=b['mtime'], size=b['size'], n=len(b['files'])),
            extra=k[0] in ARCADE or k[0] in SYSTEM_DATA,
            can_ab=bool(a) and plan(a, a_dev, b_dev, b) is not None,
            can_ba=bool(b) and plan(b, b_dev, a_dev, a) is not None))
    return rows


def transfer(src, dst, system, key):
    u = scan(src, system).get((system, key))
    if not u:
        raise ValueError('Save not found on source device')
    old = scan(dst, system).get((system, key))
    pairs = plan(u, src, dst, old)
    if pairs is None:
        raise ValueError("This save's format can't move between these two devices")
    sfs, dfs = fs_for(src), fs_for(dst)
    mtimes = {f['rel']: f['mtime'] for f in u['files']}
    stamp = time.strftime('%Y-%m-%d_%H%M%S')
    backed = 0
    # never overwrite without a backup
    for f in (old['files'] if old else []):
        dfs.write(f"{BACKUP_DIR}/{stamp}/{f['rel']}", dfs.read(f['rel']), f['mtime'])
        backed += 1
    for s_rel, d_rel in pairs:
        data = sfs.read(s_rel)
        dfs.write(d_rel, data, mtimes[s_rel])
        if dfs.lossy_times:
            folder = d_rel.rsplit('/', 1)[0]
            listed = dfs.list_many([folder]).get(folder, [])
            now = next((m for r, s, m in listed if r == d_rel), time.time())
            ledger_note(dst, d_rel, len(data), mtimes[s_rel], now)
    return dict(copied=len(pairs), backed_up=backed)
