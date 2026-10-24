# Snapchat Memories Organizer

Turn a Snapchat **"My Data"** export into a clean, date-ordered photo and video archive on your own drive — with the real capture dates and GPS restored, and your captions and stickers burned back in.

Snapchat now caps free Memories storage at 5 GB. This gets everything off their servers and onto a disk you control.

```
archive/
  2016/
    2016-07-22_200201_photo.jpg          <- caption/stickers burned in, as seen in Snapchat
    2016-07-30_100240_video.mp4
    Originals/
      2016-07-22_200201_photo.jpg        <- clean, no overlay
      2016-07-30_100240_video.mp4
  2017/
  ...
  _report.csv                            <- one row per memory: date, GPS, how it was matched
```

Files sort into true chronological order by name. Every file carries its real capture time and GPS in EXIF (photos) or container metadata (videos), plus matching filesystem timestamps — so importing into Google Photos, Apple Photos or Immich puts them on the correct day automatically.

---

## Why this exists

A raw Snapchat export is close to unusable:

- Media filenames carry **only a date**, no time, and that date is **UTC** — so it can be a day off from when you actually took it.
- Your **captions and stickers are stored as separate overlay files**, not applied to the image.
- There is **no embedded capture date or GPS** in any of the media.
- The real metadata sits in a separate `memories_history.json` with **no ID field linking it to the files**.

This tool rebuilds the connection and produces something you'd actually want to keep.

---

## Requirements

- **Python 3.9+** with [Pillow](https://pypi.org/project/Pillow/) and [piexif](https://pypi.org/project/piexif/)
- **ffmpeg and ffprobe** on your `PATH`

```bash
pip install Pillow piexif
pip install tzdata      # Windows only, and only if you use --tz (no built-in IANA database)
```

Developed and verified on Windows 11 against a real 2,793-memory export. The code is cross-platform; on macOS/Linux everything works except setting the file *Created* date, which is a Windows-only concept (Modified is still set).

---

## Step 1 — Request your export

At [accounts.snapchat.com](https://accounts.snapchat.com) on desktop → **My Data**:

1. Turn **ON** "Export your Memories"
2. Turn **ON** "Export JSON files" ← **required**. The JSON is the only source of real capture dates and GPS. Without it every file is an undated blob.
3. Leave the date-range toggle **OFF** to get everything
4. Confirm your email and submit

Snapchat says it can take **up to 7 days** to generate it, but for my 7 GB it was an hour. Snapchat does not publish a link expiry; community reports put it at **about 7 days**, so download promptly.

> **Download every ZIP listed.** A large library is split across several. Only the first (unnumbered) one contains the `json/` folder. Take only that one and you get dates without most photos; skip it and you get photos with no dates.

---

## Step 2 — Extract, inspect, run

```bash
# 1. unpack the zips (keeps the timestamps the archive depends on)
python snap_organize.py extract ./zips ./export

# 2. read-only sanity check — ALWAYS do this before step 3
python snap_organize.py inspect ./export

# 3. build the archive
python snap_organize.py run ./export ./archive
```

**Read the `inspect` output before running.** The line that matters:

```
Match test: 2793 / 2793 media matched to a JSON entry
   via mtime        2793
```

That ratio should be near 100%, and on a Snapchat export the matches should all be `mtime`. If it is low, **stop** — almost always a ZIP that was never extracted. Running anyway produces an archive dated from file timestamps alone, which is much less trustworthy.

`run` is resumable and skips files that already exist, so if it is interrupted just run it again. Use `--force` to rebuild regardless.

### Options

| Flag | Effect |
|---|---|
| `--dry-run` | Plan only, write nothing |
| `--limit N` | Process only the first N (good for a first look) |
| `--force` | Rebuild files that already exist |
| `--jobs N` | Parallelism (default: half your cores) |
| `--tz America/New_York` | Use a specific timezone instead of this machine's |
| `--utc` | Name files in UTC |
| `--crf 16` | Better video quality, bigger files (default 18) |
| `--quality 95` | JPEG quality for merged photos |
| `--raw-originals` | `Originals/` are byte-exact copies of the export (no embedded date/GPS; file timestamps still set) |

---

## What a real 2026 export actually looks like

Verified against a genuine 4-ZIP, 7.3 GB export in September 2026. **Most guides and scripts online describe an older format and no longer apply.**

```
mydata~<id>.zip        -> json/memories_history.json, html/, index.html, memories/
mydata~<id>-2.zip      -> memories/   (only)
mydata~<id>-3.zip      -> memories/   (only)
mydata~<id>-4.zip      -> memories/   (only)
```

```
memories/2024-03-16_A1B2C3D4-E5F6-47A8-9B0C-1D2E3F4A5B6C-main.jpg
memories/2024-03-16_A1B2C3D4-E5F6-47A8-9B0C-1D2E3F4A5B6C-overlay.png
```

`memories_history.json` holds exactly five fields per memory:

```json
{
  "Date": "2024-03-15 18:22:07 UTC",
  "Media Type": "Image",
  "Location": "Latitude, Longitude: 37.774900, -122.41940",
  "Download Link": "",
  "Media Download Url": ""
}
```

Two consequences:

- **The download links are empty.** Media now ships inside the ZIPs. Older tools that POST to those links to fetch media are dead.
- **There is no ID field.** The UUID in the filename appears nowhere in the JSON, so media can only be matched to metadata **by timestamp**.

---

## Landmines

Every one of these was hit building this, and every one **fails silently** — producing an archive that looks fine and is subtly wrong. If you are re-implementing this, these are the parts that matter.

### 1. `zipfile.extract()` destroys the only usable timestamp

Filenames carry a date but no time. The **time of day is stored only in the ZIP entry**, and Python's `extract()` / `extractall()` stamp every file with *now*. Lose it and there is nothing left to match on — every memory silently gets the wrong time.

Extract manually and apply `date_time` yourself (this is what `extract` does). Verify: extracted files should show a spread of times, not all identical.

### 2. Video rotation — the overlay lands sideways

Phone video is typically stored **landscape** (1280×720) with a rotation flag making it display **portrait** (720×1280). ffmpeg auto-rotates on decode, so scaling the overlay to the dimensions `ffprobe` reports stretches the caption across a sideways frame.

Swap width/height when `|rotation| % 180 == 90`.

**This hit 339 of 375 captioned videos.** Only newer clips (no rotation flag) looked right — so checking recent files alone would have missed it entirely. **Always check your oldest media, not just your newest.**

### 3. A fixed UTC offset puts half your library an hour off

Converting UTC to local with an offset captured from *today* (`datetime.now().astimezone().tzinfo`) applies summer time to winter dates. Memories from the other half of the year come out an hour wrong — in filenames *and* EXIF.

Use a DST-aware conversion that resolves the offset **per date** (`datetime.fromtimestamp()`, or `zoneinfo`). **This affected 865 of 2,793 memories** and is invisible unless you compare the filename against the file's own modification time.

### 4. `-overlay.png` is usually not a PNG

In one real export: **566 WebP**, **136 actual PNG**, and **1** in an undocumented Snapchat-internal format (`SCOF` magic bytes) that nothing can decode.

Sniff content, never trust the extension. Pillow does this automatically. Handle the undecodable case by writing the photo *without* its caption rather than failing — one lost caption beats one lost memory.

### 5. Identical timestamps are real — never deduplicate

Saving a multi-snap burst stamps every clip with the **same second and GPS**. One real case: four different 3.2 MB videos all at the same second.

Give collisions a `_2`, `_3` suffix. Several popular scripts overwrite here and silently lose the rest.

### 6. Filenames are UTC dates

`2024-03-16_....jpg` may be the evening of **March 15** local time. The archive uses local time, so filenames legitimately disagree with the export. This is the single best thing to confirm with the person you're doing this for: ask whether their oldest memory was really that evening.

### 7. Not every overlay has a photo, not every record has a file

- **Orphan overlays** — a caption layer with no base media. Keep it rather than crash.
- **JSON rows with no media file** — 186 of 2,978 in the real export. Memories deleted from the account long ago, metadata row left behind. Not a bug, and not recoverable. Say so plainly rather than quietly dropping them.

### 8. The matcher can be fooled by digits and bursts

Two bugs found only by auditing every row of `_report.csv`, not by looking at output:

- **A UUID that starts with digits looks like a time.** `2019-05-04_18304522-…` was parsed as 18:30:45. The matcher then searched around a time that never existed, pushed one clip onto a *deleted* memory's row 22 hours away, and swapped the names of two others. Only trust a time-of-day in a filename when it is not followed by more letters or digits.
- **A fixed-size search window starves a burst.** Searching ±4 rows around the insertion point works until seven clips share one second; the last one finds every neighbour already taken and goes unmatched. Walk outward by *time*, not by row count.

On a Snapchat export the `matched` column of `_report.csv` should be essentially all `mtime`. More than a handful of `mtime-loose` or `NO-JSON-MATCH` means one of these is back.

### 9. Odds and ends

- A few overlays are a slightly different shape than the photo (828×1548 over 960×1700). Scale-to-cover and centre-crop; stretching visibly widens the text.
- `memories.html` appears in **every** ZIP with different content, so file counts across ZIPs won't match the extracted total. Not data loss.
- ZIP mtimes have no timezone and can sit at a constant offset. The matcher measures a candidate shift and **keeps it only if it actually improves the match count**.
- Some overlays are **fully transparent** — a ~2 KB WebP with alpha 0 everywhere, exported for a snap that had no caption. The merged file is then (correctly) identical to the original. A caption-visibility check will flag these; look at the overlay's alpha channel before concluding the composite failed. Two of 703 in the real export.
- Stream-copying originals is a remux: frames and audio are untouched, but the container is rewritten. A source whose own headers disagree (one in 2,793 here: a 0.5 s video stream with 3.5 s of audio) can report a different length afterwards. `--raw-originals` sidesteps this entirely.

---

## Verifying the result

Do not trust a "0 errors" summary. It only means nothing threw.

**Automated:**

- Every merged file has a twin in `Originals/`. For captioned memories, diff the two (first frame, for video). A caption is a **large** delta (max channel difference well over 100); a plain JPEG/H.264 re-encode is small noise everywhere (under ~40). Identical pixels means the caption never got applied (or the overlay itself is empty — check its alpha channel) — but a mere difference does **not** prove it did, so threshold it, don't just test for "any difference".
- For captioned videos, the output frame's aspect ratio should match the overlay's — catches landmine #2.
- **Each file's modification time should equal the time in its own filename.** This is what catches landmine #3, and nothing else does.
- Every original video should display at the same dimensions as its source — confirms the rotation flag survived the remux.
- Read the `matched` column of `_report.csv`, every row — this is what catches landmine #8. Then look at each non-exact row's source file and ask why.
- Check EXIF `DateTimeOriginal`, `OffsetTimeOriginal` and GPS against `_report.csv` — for every photo, not a sample; it's a few seconds. `DateTimeOriginal` minus the offset should equal `date_utc`.

**By eye — have the person check both ends of the timeline:**

1. Their **oldest** memory and their **newest** (different code paths, as #2 proved)
2. A **captioned video** and its clean twin
3. A **sticker-heavy** memory
4. In Explorer/Finder, that **Date taken** matches the filename

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Low match rate in `inspect` | A ZIP was never extracted, or timestamps were destroyed during extraction (landmine #1) |
| All files dated the same day | Extracted with a tool that discarded timestamps — re-extract with `extract` |
| Captions stretched/sideways on video | Rotation not handled (landmine #2) |
| Dates an hour off for part of the year | Fixed UTC offset instead of DST-aware (landmine #3) |
| "no memories_history.json found" | The unnumbered ZIP wasn't extracted, or "Export JSON files" was off when requesting |
| Photos with no caption applied | Check `_report.csv` `has_overlay` — the export may genuinely have no overlay |
| `--tz` fails with `ZoneInfoNotFoundError` | Windows has no IANA database: `pip install tzdata` |
| A few `mtime-loose` / `NO-JSON-MATCH` rows | Landmine #8 — inspect those rows' source filenames |
| Photos hours off in Immich, but only those without GPS or taken abroad | EXIF written without `OffsetTimeOriginal` by an older version — re-run with `--force` |

---

## Notes

- Only merged videos are re-encoded (unavoidable when burning in an overlay), at CRF 18. Videos without overlays and all originals are stream-copied, losing no quality.
- Video `creation_time` is written in UTC, per the MP4 spec. Photo EXIF is written in local time, per the EXIF spec. They are supposed to differ.
- Photo EXIF also carries `OffsetTimeOriginal` (e.g. `+02:00`), so the local time is unambiguous. Without it, readers have to guess the zone: Immich takes it from the GPS position (wrong for photos taken abroad, since the time is in *your* zone) and assumes UTC when there is no GPS. On one 1,072-memory export that put 113 photos 1–3 hours off on the timeline.
- AR lenses and face filters were applied at capture and live in the base media, so they appear in both copies.
- The tool never deletes or modifies your export. It only reads from it.

## License

MIT
