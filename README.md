# Annotated Dailies Export for OpenRV

An [OpenRV](https://github.com/AcademySoftwareFoundation/OpenRV) package that
takes a playlist of dailies you've annotated in RV and exports **each clip as its
own movie with the annotations burned in**, plus a still of every annotated frame.

Works on Linux, Windows and macOS.

## What you get

For every clip in the playlist that has annotations:

| File | Contents |
|---|---|
| `<clip>_notes.mov` | The full clip with annotations burned in |
| `<clip>_f<frame>.jpg` | One still per annotated frame (source frame number) |
| `annotated_export.log` | The rvio commands that were run, and any errors |

## Install

1. Download the latest `.rvpkg` from [Releases](../../releases).
2. In RV: **Preferences › Packages › Add Packages…**, pick the file, tick
   **Installed** and **Load**.
3. Restart RV. An **Annotated Dailies** menu appears.

Or from a shell:

```bash
rvpkg -install -add <your RV support path> annotated_dailies_export-0.1.rvpkg
```

### rvio

The package renders with `rvio`, which ships with OpenRV. It looks next to the
`rv` binary first, then on your `PATH`. If it isn't found, set `RVIO_PATH`:

```bash
# Linux / macOS (~/.bashrc or ~/.zshrc)
export RVIO_PATH=/opt/openrv/bin/rvio
```

```bat
:: Windows
setx RVIO_PATH "D:\apps\openRV\bin\rvio.exe"
```

## Use

1. Load your dailies (they play as a sequence).
2. Annotate frames with RV's annotation tool.
3. **Annotated Dailies › Export Annotated Clips...** (or **Export All Clips...**)
   and choose an output folder.

Rendering runs in the background; check `annotated_export.log` for progress.

## How it works

1. Walks the playlist frame by frame and records which source plays on each
   frame and its source-relative frame number.
2. Reads each source's `RVPaint` node to find which frames have strokes.
3. Saves a temporary copy of the session and runs `rvio session.rv -t start-end`
   once per clip, and once per annotated still.

## Known limitations

- An annotation appears on a single frame of the movie, which flashes by at
  playback speed — that's why stills are exported too.
- Output codecs depend on how your OpenRV / FFmpeg was built.
- Sessions moved between Linux and Windows need media paths that resolve on the
  machine running the export.

## Build from source

```bash
python build.py        # writes dist/annotated_dailies_export-<version>.rvpkg
```

Bump `version:` in `src/PACKAGE`, commit, then tag to publish a release:

```bash
git tag v0.2 && git push origin v0.2
```

## License

Apache-2.0 (same as OpenRV).
