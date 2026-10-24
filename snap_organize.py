#!/usr/bin/env python3
"""
snap_organize.py - turn a Snapchat "My Data" export into a date-ordered archive.

Layout produced:
    <out>/2016/2016-04-02_193344_photo.jpg           <- overlay burned in (as seen in Snapchat)
    <out>/2016/Originals/2016-04-02_193344_photo.jpg <- clean, no overlay
    <out>/_report.csv                                <- one row per memory

Capture date + GPS are written into EXIF (photos) and container metadata
(videos), and into the Created/Modified timestamps.

Usage:
    python snap_organize.py extract ./zips ./export     # unpack, keeping timestamps
    python snap_organize.py inspect ./export            # read-only sanity check
    python snap_organize.py run ./export ./archive      # build the archive

Run `inspect` before `run` and check the match rate. If it is low, stop:
something is wrong (usually a zip that was never extracted).
"""

import argparse
import csv
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from threading import Lock

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".avi"}
OVERLAY_RE = re.compile(r"[-_]overlay$", re.I)
MAIN_RE = re.compile(r"[-_](main|media)$", re.I)
# A full datetime in the filename is only trusted when the seconds are not
# followed by more letters or digits. Snapchat names are DATE_UUID, and a UUID
# that happens to begin with digits (2019-05-04_18304522-...) would otherwise
# read as 18:30:45 and send the matcher hunting around a time that never was.
FNAME_DT_RES = [
    re.compile(r"(\d{4})[-_]?(\d{2})[-_]?(\d{2})[ _T]+(\d{2})[-_:]?(\d{2})[-_:]?(\d{2})"
               r"(?![0-9A-Za-z])"),
    re.compile(r"(\d{4})[-_](\d{2})[-_](\d{2})"),
]
MATCH_TOLERANCE = timedelta(seconds=90)   # exact match
LOOSE_TOLERANCE = timedelta(days=1)       # fallback, only if the filename date agrees

PRINT_LOCK = Lock()


def log(msg):
    with PRINT_LOCK:
        print(msg, flush=True)


def run_cmd(args):
    return subprocess.run(args, capture_output=True, text=True, errors="replace")


def ffprobe_json(path):
    r = run_cmd(["ffprobe", "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-show_format", str(path)])
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def set_windows_times(path, dt_utc):
    """Set Created + Modified + Accessed to dt_utc."""
    ts = dt_utc.timestamp()
    try:
        os.utime(path, (ts, ts))
    except Exception:
        pass
    if os.name != "nt":
        return
    EPOCH_AS_FILETIME = 116444736000000000
    HUNDREDS_OF_NS = 10000000
    ft = int(ts * HUNDREDS_OF_NS) + EPOCH_AS_FILETIME
    if ft < 0:
        return

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                    ("dwHighDateTime", ctypes.c_uint32)]

    t = FILETIME(ft & 0xFFFFFFFF, ft >> 32)
    k = ctypes.windll.kernel32
    k.CreateFileW.restype = ctypes.c_void_p
    h = k.CreateFileW(str(path), 0x100, 7, None, 3, 0x02000000, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        return
    try:
        k.SetFileTime(ctypes.c_void_p(h), ctypes.byref(t),
                      ctypes.byref(t), ctypes.byref(t))
    finally:
        k.CloseHandle(ctypes.c_void_p(h))


# --------------------------------------------------------------- parse export

class Entry:
    __slots__ = ("dt_utc", "media_type", "lat", "lon", "ident", "raw", "used")

    def __init__(self, dt_utc, media_type, lat, lon, ident, raw):
        self.dt_utc = dt_utc
        self.media_type = media_type
        self.lat = lat
        self.lon = lon
        self.ident = ident
        self.raw = raw
        self.used = False

    @property
    def is_video(self):
        return "video" in (self.media_type or "").lower()


def to_local(dt_utc, tzinfo):
    """UTC -> local wall-clock time.

    tzinfo None means "this machine's zone, resolved per date". That matters:
    a fixed offset taken from today would put every winter memory an hour late,
    because the offset in January is not the offset in July.

    The result is always offset-aware, so the EXIF writer can record which
    offset the wall-clock time belongs to.
    """
    if tzinfo is None:
        return dt_utc.astimezone()
    return dt_utc.astimezone(tzinfo)


def parse_dt(value):
    if value is None or value == "":
        return None
    s = str(value).strip().replace("UTC", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        n = float(s)
        if n > 1e11:
            n = n / 1000.0
        if n > 1e8:
            return datetime.fromtimestamp(n, timezone.utc)
    except Exception:
        pass
    return None


def parse_location(rec):
    """Snapchat writes Location = 'Latitude, Longitude: 37.77, -122.41'."""
    for k, v in rec.items():
        if not isinstance(v, str):
            continue
        kl = k.lower()
        vl = v.lower()
        looks_geo = ("location" in kl or "latitude" in kl
                     or ("latitude" in vl and "longitude" in vl))
        if looks_geo:
            m = re.search(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", v)
            if m:
                lat, lon = float(m.group(1)), float(m.group(2))
                if lat or lon:
                    return lat, lon
    lat = lon = None
    for k, v in rec.items():
        kl = k.lower().strip()
        if kl in ("latitude", "lat"):
            try:
                lat = float(v)
            except Exception:
                pass
        elif kl in ("longitude", "lon", "lng"):
            try:
                lon = float(v)
            except Exception:
                pass
    if lat is not None and lon is not None and (lat or lon):
        return lat, lon
    return None, None


def find_memories_json(export_dir):
    hits = sorted(Path(export_dir).rglob("memories_history*.json"))
    return hits


def load_entries(json_paths):
    entries = []
    for jp in json_paths:
        try:
            data = json.loads(Path(jp).read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            log("  ! could not read %s: %s" % (jp, e))
            continue
        records = []
        if isinstance(data, list):
            records = [r for r in data if isinstance(r, dict)]
        elif isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    records.extend(v)
        for rec in records:
            dt = None
            for k in rec:
                kl = k.lower()
                if "date" in kl or "time" in kl:
                    dt = parse_dt(rec[k])
                    if dt:
                        break
            if not dt:
                continue
            mtype = ""
            for k in rec:
                if "media type" in k.lower() or k.lower() == "type":
                    mtype = str(rec[k])
                    break
            lat, lon = parse_location(rec)
            ident = ""
            for k in rec:
                if k.lower() in ("mid", "media id", "memory id", "id"):
                    ident = str(rec[k]).strip()
                    break
            entries.append(Entry(dt, mtype, lat, lon, ident, rec))
    entries.sort(key=lambda e: e.dt_utc)
    return entries


class Pair:
    __slots__ = ("stem", "main", "overlay", "key_dt", "key_src", "entry", "day_hint")

    def __init__(self, stem, main, overlay):
        self.stem = stem
        self.main = main
        self.overlay = overlay
        self.key_dt = None
        self.key_src = None
        self.entry = None
        self.day_hint = None

    @property
    def is_video(self):
        return self.main.suffix.lower() in VIDEO_EXT


def dt_from_filename(name):
    """Returns (datetime, precise). A date-only name is not precise enough to
    match on directly - it lands at midnight, hours from the real capture time."""
    for rx in FNAME_DT_RES:
        m = rx.search(name)
        if m:
            g = [x for x in m.groups() if x]
            try:
                if len(g) >= 6:
                    return datetime(*[int(x) for x in g[:6]], tzinfo=timezone.utc), True
                if len(g) >= 3:
                    return datetime(*[int(x) for x in g[:3]], tzinfo=timezone.utc), False
            except ValueError:
                pass
    return None, False


def scan_media(export_dir):
    """Collect media files and pair -main with -overlay."""
    root = Path(export_dir).resolve()
    groups = {}
    loose = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext not in PHOTO_EXT and ext not in VIDEO_EXT:
            continue
        rel_parts = [x.lower() for x in p.relative_to(root).parts[:-1]]
        in_memories = any("memor" in x for x in rel_parts)
        if not in_memories and rel_parts:
            continue  # skip html assets, thumbnails, chat media, etc.
        stem = p.stem
        if OVERLAY_RE.search(stem):
            groups.setdefault(OVERLAY_RE.sub("", stem), {})["overlay"] = p
        elif MAIN_RE.search(stem):
            groups.setdefault(MAIN_RE.sub("", stem), {})["main"] = p
        else:
            loose.append(p)

    pairs = []
    for stem, g in groups.items():
        if "main" in g:
            pairs.append(Pair(stem, g["main"], g.get("overlay")))
        elif "overlay" in g:
            pairs.append(Pair(stem, g["overlay"], None))
    for p in loose:
        pairs.append(Pair(p.stem, p, None))

    for pr in pairs:
        dt, precise = dt_from_filename(pr.main.name)
        if dt and precise:
            pr.key_dt, pr.key_src = dt, "filename"
        else:
            # A date-only filename still pins the day; keep it as a guard, but
            # match on mtime, which carries the actual time of day.
            pr.day_hint = dt.date() if dt else None
            pr.key_dt = datetime.fromtimestamp(pr.main.stat().st_mtime, timezone.utc)
            pr.key_src = "mtime"
    pairs.sort(key=lambda x: x.key_dt)
    return pairs


def match_pairs(entries, pairs):
    """Match media to JSON entries, correcting for a constant clock offset."""
    ids = {e.ident: e for e in entries if e.ident and len(e.ident) >= 8}
    remaining = []
    for pr in pairs:
        hit = None
        for i, e in ids.items():
            if not e.used and i in pr.stem:
                hit = e
                break
        if hit is not None:
            hit.used = True
            pr.entry = hit
            pr.key_src = "id"
        else:
            remaining.append(pr)

    free = [e for e in entries if not e.used]
    if not free or not remaining:
        return
    times = [e.dt_utc for e in free]

    def nearest(target, skip_used=True):
        """Closest entry to target. Walks outward from the insertion point by
        TIME rather than a fixed number of slots, so a burst of many memories
        saved in the same second cannot crowd the last one out of the window."""
        i = bisect_left(times, target)
        best, best_gap = None, None
        for j0, stop, step in ((i - 1, -1, -1), (i, len(free), 1)):
            for j in range(j0, stop, step):
                e = free[j]
                gap = abs(e.dt_utc - target)
                if gap > LOOSE_TOLERANCE:
                    break
                if skip_used and e.used:
                    continue
                if best_gap is None or gap < best_gap:
                    best, best_gap = e, gap
        return best, best_gap

    # A zip stores mtimes without a timezone, so the whole set can sit at a
    # constant offset. Measure a candidate shift, then keep it only if it
    # genuinely lines more files up than leaving it alone.
    deltas = []
    for pr in remaining:
        i = bisect_left(times, pr.key_dt)
        for j in (i - 1, i):
            if 0 <= j < len(times):
                deltas.append((pr.key_dt - times[j]).total_seconds())
    offset = timedelta(0)
    if deltas:
        deltas.sort()
        cand = timedelta(seconds=deltas[len(deltas) // 2])
        if abs(cand.total_seconds()) > 120:
            def score(off):
                n = 0
                for pr in remaining:
                    _, gap = nearest(pr.key_dt - off, skip_used=False)
                    if gap is not None and gap <= MATCH_TOLERANCE:
                        n += 1
                return n
            if score(cand) > score(timedelta(0)):
                offset = cand
                log("  clock offset in export: %+.2f h - correcting"
                    % (cand.total_seconds() / 3600.0))

    for pr in sorted(remaining, key=lambda x: x.key_dt):
        target = pr.key_dt - offset
        best, best_gap = nearest(target)
        if best is None:
            continue
        if best_gap <= MATCH_TOLERANCE:
            best.used, pr.entry = True, best
        elif best_gap <= LOOSE_TOLERANCE:
            # Looser fallback, but only if the filename's date agrees with it.
            if pr.day_hint is None or pr.day_hint == best.dt_utc.date():
                best.used, pr.entry, pr.key_src = True, best, "mtime-loose"


# ------------------------------------------------------------------ metadata

def _rat(x):
    f = Fraction(abs(x)).limit_denominator(100000)
    return (f.numerator, f.denominator)


def _dms(deg):
    deg = abs(deg)
    d = int(deg)
    m = int((deg - d) * 60)
    s = (deg - d - m / 60.0) * 3600
    return ((d, 1), (m, 1), _rat(round(s, 4)))


def write_photo_metadata(path, dt_local, lat, lon):
    """Embed capture time + GPS into a JPEG."""
    import piexif
    if Path(path).suffix.lower() not in (".jpg", ".jpeg"):
        return False
    try:
        try:
            ex = piexif.load(str(path))
        except Exception:
            ex = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}
        for key in ("0th", "Exif", "GPS"):
            ex.setdefault(key, {})
        ex.pop("thumbnail", None)
        ex.pop("1st", None)
        stamp = dt_local.strftime("%Y:%m:%d %H:%M:%S").encode()
        ex["0th"][piexif.ImageIFD.DateTime] = stamp
        ex["Exif"][piexif.ExifIFD.DateTimeOriginal] = stamp
        ex["Exif"][piexif.ExifIFD.DateTimeDigitized] = stamp
        # Without an offset, readers guess the zone: Immich takes it from GPS
        # (wrong when abroad) or assumes UTC when there is no GPS.
        off = dt_local.utcoffset()
        if off is not None:
            mins = int(off.total_seconds() // 60)
            offset = ("%s%02d:%02d" % ("-" if mins < 0 else "+",
                                       abs(mins) // 60, abs(mins) % 60)).encode()
            ex["Exif"][piexif.ExifIFD.OffsetTime] = offset
            ex["Exif"][piexif.ExifIFD.OffsetTimeOriginal] = offset
            ex["Exif"][piexif.ExifIFD.OffsetTimeDigitized] = offset
        if lat is not None and lon is not None:
            ex["GPS"] = {
                piexif.GPSIFD.GPSLatitudeRef: (b"N" if lat >= 0 else b"S"),
                piexif.GPSIFD.GPSLatitude: _dms(lat),
                piexif.GPSIFD.GPSLongitudeRef: (b"E" if lon >= 0 else b"W"),
                piexif.GPSIFD.GPSLongitude: _dms(lon),
            }
        piexif.insert(piexif.dump(ex), str(path))
        return True
    except Exception as e:
        log("    ! exif failed on %s: %s" % (Path(path).name, e))
        return False


def video_meta_args(dt_utc, lat, lon):
    args = ["-metadata",
            "creation_time=" + dt_utc.strftime("%Y-%m-%dT%H:%M:%S.000000Z")]
    if lat is not None and lon is not None:
        iso = "%+08.4f%+09.4f/" % (lat, lon)
        # ffmpeg writes these as the (c)xyz / loci atoms, the de-facto MP4
        # location tags. (A com.apple.quicktime.* key would be silently
        # dropped without -movflags use_metadata_tags, so none is passed.)
        args += ["-metadata", "location=" + iso,
                 "-metadata", "location-eng=" + iso]
    return args


# ------------------------------------------------------------------ processing

def fit_cover(img, size):
    """Resize to fill `size` without distorting. A few Snapchat overlays are a
    slightly different shape from the photo (e.g. 828x1548 over 960x1700);
    stretching them to fit widens the text, so scale to cover and centre-crop
    instead - the cropped margin is transparent anyway."""
    from PIL import Image
    tw, th = size
    w, h = img.size
    if abs(w / h - tw / th) < 0.01:
        return img.resize(size, Image.LANCZOS)
    scale = max(tw / w, th / h)
    nw, nh = max(tw, int(round(w * scale))), max(th, int(round(h * scale)))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def composite_photo(main_path, overlay_path, dest, quality=95):
    """Burn the overlay into the photo. Returns "" on success, or a reason
    string if the overlay could not be read - a handful of Snapchat overlays
    are an internal format (SCOF) rather than a real image. In that case the
    photo is still written, just without its caption, so no memory goes
    missing from the archive."""
    from PIL import Image
    why = ""
    with Image.open(main_path) as base_img:
        base = base_img.convert("RGBA")
        if overlay_path is not None:
            try:
                with Image.open(overlay_path) as ov_img:
                    ov = ov_img.convert("RGBA")
                    if ov.size != base.size:
                        ov = fit_cover(ov, base.size)
                    base = Image.alpha_composite(base, ov)
            except Exception as e:
                why = "overlay unreadable (%s)" % type(e).__name__
        base.convert("RGB").save(dest, "JPEG", quality=quality, subsampling=0)
    return why


def composite_video(main_path, overlay_path, dest, meta_args, crf=18):
    info = ffprobe_json(main_path)
    width = height = None
    rotation = 0
    has_audio = False
    if info:
        for s in info.get("streams", []):
            if s.get("codec_type") == "video" and width is None:
                width, height = s.get("width"), s.get("height")
                # Phone video is usually stored landscape with a rotation flag.
                # ffmpeg auto-rotates on decode, so the frame the overlay lands
                # on is the ROTATED size - scaling to the stored size stretches
                # the caption across a sideways frame.
                for sd in s.get("side_data_list") or []:
                    if "rotation" in sd:
                        try:
                            rotation = int(float(sd["rotation"]))
                        except (TypeError, ValueError):
                            pass
                if not rotation:
                    try:
                        rotation = int(float(s.get("tags", {}).get("rotate", 0)))
                    except (TypeError, ValueError):
                        rotation = 0
            if s.get("codec_type") == "audio":
                has_audio = True
    if not width or not height:
        return False, "ffprobe could not read video dimensions"
    if abs(rotation) % 180 == 90:
        width, height = height, width

    filt = ("[1:v]scale=%d:%d[ovl];[0:v][ovl]overlay=0:0:format=auto" % (width, height))
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-i", str(main_path), "-i", str(overlay_path),
           "-filter_complex", filt,
           "-c:v", "libx264", "-crf", str(crf), "-preset", "medium",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
    cmd += ["-c:a", "copy"] if has_audio else ["-an"]
    cmd += meta_args + [str(dest)]
    r = run_cmd(cmd)
    if r.returncode != 0 and has_audio:
        cmd2 = [c for c in cmd]
        i = cmd2.index("-c:a")
        cmd2[i + 1] = "aac"
        r = run_cmd(cmd2)
    if r.returncode != 0:
        return False, (r.stderr or "ffmpeg failed").strip().splitlines()[-1:][0][:200]
    return True, ""


def copy_video_with_meta(main_path, dest, meta_args):
    """Remux (no re-encode) so we can stamp metadata into the container."""
    if main_path.suffix.lower() in (".mp4", ".mov", ".m4v"):
        cmd = (["ffmpeg", "-y", "-loglevel", "error", "-i", str(main_path),
                "-c", "copy", "-movflags", "+faststart"] + meta_args + [str(dest)])
        r = run_cmd(cmd)
        if r.returncode == 0:
            return True
    shutil.copy2(main_path, dest)
    return False


def process_one(pr, out_root, opts, name):
    """Returns a report row dict."""
    e = pr.entry
    dt_utc = e.dt_utc if e else pr.key_dt
    dt_local = to_local(dt_utc, opts.tzinfo)
    lat = e.lat if e else None
    lon = e.lon if e else None

    year_dir = out_root / ("%04d" % dt_local.year)
    orig_dir = year_dir / "Originals"
    is_video = pr.is_video
    merged_ext = ".mp4" if is_video else ".jpg"
    main_ext = pr.main.suffix.lower()

    merged_path = year_dir / (name + merged_ext)
    orig_path = orig_dir / (name + main_ext)

    row = {
        "date_local": dt_local.strftime("%Y-%m-%d %H:%M:%S"),
        "date_utc": dt_utc.strftime("%Y-%m-%d %H:%M:%S"),
        "type": "video" if is_video else "photo",
        "matched": pr.key_src if e else "NO-JSON-MATCH",
        "has_overlay": "yes" if pr.overlay else "no",
        "gps": ("%.5f, %.5f" % (lat, lon)) if lat is not None else "",
        "output": str(merged_path.relative_to(out_root)),
        "source": pr.main.name,
        "status": "ok",
        "note": "",
    }

    if opts.dry_run:
        row["status"] = "dry-run"
        return row

    year_dir.mkdir(parents=True, exist_ok=True)
    orig_dir.mkdir(parents=True, exist_ok=True)
    meta = video_meta_args(dt_utc, lat, lon)

    # ---- clean original
    try:
        if not (orig_path.exists() and not opts.force):
            if opts.raw_originals:
                shutil.copy2(pr.main, orig_path)      # byte-exact, nothing embedded
            elif is_video:
                copy_video_with_meta(pr.main, orig_path, meta)
            else:
                shutil.copy2(pr.main, orig_path)
                write_photo_metadata(orig_path, dt_local, lat, lon)
            set_windows_times(orig_path, dt_utc)
    except Exception as ex:
        row["status"] = "error"
        row["note"] = "original: %s" % ex

    # ---- merged (overlay burned in)
    try:
        if merged_path.exists() and not opts.force:
            # Resumed run: this file was made earlier. Say so rather than
            # claiming "ok" for work this run did not do or check.
            row["status"] = "present"
            return row
        if pr.overlay is None:
            # nothing to burn in: the merged copy is the original
            if is_video:
                copy_video_with_meta(pr.main, merged_path, meta)
            else:
                if main_ext in (".jpg", ".jpeg"):
                    shutil.copy2(pr.main, merged_path)
                else:
                    composite_photo(pr.main, None, merged_path, opts.quality)
                write_photo_metadata(merged_path, dt_local, lat, lon)
        else:
            if is_video:
                ok, err = composite_video(pr.main, pr.overlay, merged_path, meta, opts.crf)
                if not ok:
                    copy_video_with_meta(pr.main, merged_path, meta)
                    row["status"] = "warn"
                    row["note"] = "overlay not burned in: %s" % err
            else:
                why = composite_photo(pr.main, pr.overlay, merged_path, opts.quality)
                write_photo_metadata(merged_path, dt_local, lat, lon)
                if why:
                    row["status"] = "warn"
                    row["note"] = "saved without its caption: %s" % why
        set_windows_times(merged_path, dt_utc)
    except Exception as ex:
        row["status"] = "error"
        row["note"] = "merged: %s" % ex
    return row


def assign_names(pairs, opts):
    """Collision-safe YYYY-MM-DD_HHMMSS names. Never silently overwrite."""
    used = set()
    names = {}
    for pr in sorted(pairs, key=lambda p: (p.entry.dt_utc if p.entry else p.key_dt,
                                           p.main.name)):
        dt_utc = pr.entry.dt_utc if pr.entry else pr.key_dt
        dt_local = to_local(dt_utc, opts.tzinfo)
        kind = "video" if pr.is_video else "photo"
        base = "%s_%s" % (dt_local.strftime("%Y-%m-%d_%H%M%S"), kind)
        name, n = base, 1
        while name.lower() in used:
            n += 1
            name = "%s_%d" % (base, n)
        used.add(name.lower())
        names[id(pr)] = name
    return names


# ------------------------------------------------------------------- commands

def cmd_inspect(args):
    export = Path(args.export)
    if not export.exists():
        log("Export folder not found: %s" % export)
        return 1
    log("Inspecting: %s\n" % export)

    tops = sorted([p for p in export.iterdir()])
    log("Top level (%d entries):" % len(tops))
    for p in tops[:40]:
        log("   %s%s" % (p.name, "/" if p.is_dir() else ""))

    jsons = find_memories_json(export)
    log("\nmemories_history JSON files found: %d" % len(jsons))
    for j in jsons:
        log("   %s  (%.1f KB)" % (j.relative_to(export), j.stat().st_size / 1024))

    entries = []
    if jsons:
        raw = json.loads(jsons[0].read_text(encoding="utf-8", errors="replace"))
        if isinstance(raw, dict):
            log("\nTop-level JSON keys: %s" % list(raw.keys()))
            for k, v in raw.items():
                if isinstance(v, list):
                    log("   %r -> list of %d" % (k, len(v)))
        sample = None
        if isinstance(raw, dict):
            for v in raw.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    sample = v[0]
                    break
        elif isinstance(raw, list) and raw:
            sample = raw[0]
        if sample:
            log("\nFields on one memory record:")
            for k, v in sample.items():
                sv = str(v)
                log("   %-24s = %s" % (k, sv[:70] + ("..." if len(sv) > 70 else "")))
        entries = load_entries(jsons)
        log("\nParsed %d dated entries." % len(entries))
        if entries:
            log("   oldest: %s UTC" % entries[0].dt_utc)
            log("   newest: %s UTC" % entries[-1].dt_utc)
            vids = sum(1 for e in entries if e.is_video)
            log("   videos: %d   photos: %d" % (vids, len(entries) - vids))
            log("   with GPS: %d" % sum(1 for e in entries if e.lat is not None))

    pairs = scan_media(export)
    log("\nMedia files paired: %d" % len(pairs))
    withov = sum(1 for p in pairs if p.overlay)
    log("   with an overlay: %d" % withov)
    log("   videos: %d   photos: %d" % (sum(1 for p in pairs if p.is_video),
                                        sum(1 for p in pairs if not p.is_video)))
    log("   date key from filename: %d, from mtime: %d"
        % (sum(1 for p in pairs if p.key_src == "filename"),
           sum(1 for p in pairs if p.key_src == "mtime")))
    log("\nSample filenames:")
    for pr in pairs[:8]:
        log("   main=%s  overlay=%s" % (pr.main.name,
                                        pr.overlay.name if pr.overlay else "-"))

    if entries and pairs:
        match_pairs(entries, pairs)
        matched = sum(1 for p in pairs if p.entry)
        log("\nMatch test: %d / %d media matched to a JSON entry" % (matched, len(pairs)))
        by_src = {}
        for p in pairs:
            if p.entry:
                by_src[p.key_src] = by_src.get(p.key_src, 0) + 1
        for k, v in sorted(by_src.items()):
            log("   via %-12s %d" % (k, v))
        if matched < len(pairs):
            log("   unmatched media will be dated from its file timestamp instead.")
    return 0


def cmd_run(args):
    export = Path(args.export)
    out = Path(args.out)
    if not export.exists():
        log("Export folder not found: %s" % export)
        return 1

    log("Reading export: %s" % export)
    jsons = find_memories_json(export)
    if not jsons:
        log("  ! no memories_history.json found - dates will come from file timestamps.")
        log("    Re-request the export with 'Export JSON files' turned ON for real dates.")
    entries = load_entries(jsons) if jsons else []
    log("  %d memories listed in the JSON" % len(entries))

    pairs = scan_media(export)
    log("  %d media files found on disk" % len(pairs))
    if not pairs:
        log("Nothing to do. Did you extract every ZIP into this folder?")
        return 1

    if entries:
        match_pairs(entries, pairs)
    matched = sum(1 for p in pairs if p.entry)
    log("  %d matched to a dated JSON entry, %d dated from file timestamp"
        % (matched, len(pairs) - matched))

    unused = [e for e in entries if not e.used]
    if unused:
        log("  ! %d JSON entries have no media file on disk "
            "(likely a ZIP you haven't extracted yet)" % len(unused))

    if args.limit:
        pairs = pairs[:args.limit]
        log("  --limit: only processing the first %d" % len(pairs))

    names = assign_names(pairs, args)
    out.mkdir(parents=True, exist_ok=True)

    log("\nWriting to: %s" % out)
    log("Timezone for folder/file dates: %s\n" % args.tzname)

    rows = []
    done = [0]
    total = len(pairs)

    def work(pr):
        return process_one(pr, out, args, names[id(pr)])

    with ThreadPoolExecutor(max_workers=args.jobs) as ex:
        futs = {ex.submit(work, pr): pr for pr in pairs}
        for f in as_completed(futs):
            pr = futs[f]
            try:
                row = f.result()
            except Exception as e:
                row = {"date_local": "", "date_utc": "", "type": "",
                       "matched": "", "has_overlay": "", "gps": "",
                       "output": "", "source": pr.main.name,
                       "status": "error", "note": str(e)}
            rows.append(row)
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == total:
                log("  %d / %d" % (done[0], total))

    rows.sort(key=lambda r: (r["date_local"], r["output"]))
    report = out / "_report.csv"
    with open(report, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    errs = [r for r in rows if r["status"] == "error"]
    warns = [r for r in rows if r["status"] == "warn"]
    log("\nDone. %d memories, %d warnings, %d errors." % (len(rows), len(warns), len(errs)))
    log("Report: %s" % report)
    for r in errs[:10]:
        log("  ERROR %s: %s" % (r["source"], r["note"]))
    if len(errs) > 10:
        log("  ... %d more in the report" % (len(errs) - 10))
    return 0


def cmd_extract(args):
    """Unpack the export zips, keeping each file's stored timestamp.

    zipfile.extract() stamps everything with 'now'. Snapchat's media filenames
    carry only a date, so the time-of-day in the zip entry is the only thing
    that can match a file to its row in memories_history.json. Lose it and
    every memory silently gets the wrong time.
    """
    import calendar
    import zipfile

    src, dst = Path(args.zips), Path(args.out)
    dst.mkdir(parents=True, exist_ok=True)
    zips = sorted(src.glob("*.zip")) if src.is_dir() else [src]
    if not zips:
        log("No .zip files in %s" % src)
        return 1
    log("Extracting %d zip(s) -> %s" % (len(zips), dst))

    done = skipped = errors = 0
    for z in zips:
        zf = zipfile.ZipFile(z)
        infos = [i for i in zf.infolist() if not i.is_dir()]
        log("\n%s  (%d entries)" % (z.name, len(infos)))
        for n, i in enumerate(infos, 1):
            out = dst / i.filename
            try:
                ts = calendar.timegm(i.date_time + (0, 0, -1))  # stored time as UTC
                if out.exists() and out.stat().st_size == i.file_size:
                    skipped += 1
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(i) as fsrc, open(out, "wb") as fdst:
                        shutil.copyfileobj(fsrc, fdst, 1 << 20)
                    done += 1
                os.utime(out, (ts, ts))
            except Exception as e:
                errors += 1
                log("  ! %s: %s" % (i.filename, e))
            if n % 250 == 0:
                log("   %d / %d" % (n, len(infos)))

    log("\nextracted %d, already present %d, errors %d" % (done, skipped, errors))
    log("Next:  python snap_organize.py inspect \"%s\"" % dst)
    return 1 if errors else 0


def main():
    ap = argparse.ArgumentParser(description="Organize a Snapchat export by date.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pe = sub.add_parser("extract", help="unpack the export zips, keeping timestamps")
    pe.add_argument("zips", help="folder holding the mydata~*.zip files (or one zip)")
    pe.add_argument("out", help="where to extract to")

    pi = sub.add_parser("inspect", help="report on the export without writing anything")
    pi.add_argument("export")

    pr = sub.add_parser("run", help="build the archive")
    pr.add_argument("export")
    pr.add_argument("out")
    pr.add_argument("--dry-run", action="store_true", help="plan only, write nothing")
    pr.add_argument("--force", action="store_true", help="redo files that already exist")
    pr.add_argument("--limit", type=int, default=0, help="only process the first N")
    pr.add_argument("--jobs", type=int, default=max(2, (os.cpu_count() or 4) // 2))
    pr.add_argument("--crf", type=int, default=18, help="video quality, lower is better")
    pr.add_argument("--quality", type=int, default=95, help="JPEG quality")
    pr.add_argument("--raw-originals", action="store_true",
                    help="Originals/ are byte-exact copies of the export "
                         "(no embedded date/GPS; file timestamps still set)")
    pr.add_argument("--tz", default=None,
                    help="timezone for dates, e.g. America/New_York (default: this PC)")
    pr.add_argument("--utc", action="store_true", help="name files in UTC instead")

    args = ap.parse_args()

    # None = this machine's zone, resolved per date (handles historical DST)
    tzinfo = None
    tzname = "%s (system, DST-aware)" % datetime.now().astimezone().tzname()
    if getattr(args, "utc", False):
        tzinfo, tzname = timezone.utc, "UTC"
    elif getattr(args, "tz", None):
        from zoneinfo import ZoneInfo
        try:
            tzinfo, tzname = ZoneInfo(args.tz), args.tz
        except Exception as e:
            log("Unknown timezone %r (%s)." % (args.tz, type(e).__name__))
            log("Windows has no built-in IANA database:  pip install tzdata")
            return 2
    args.tzinfo, args.tzname = tzinfo, tzname

    if args.cmd == "extract":
        return cmd_extract(args)
    if args.cmd == "inspect":
        args.dry_run = True
        return cmd_inspect(args)
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
