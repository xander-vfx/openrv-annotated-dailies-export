"""
Annotated Dailies Export - an OpenRV Python mode.

Menu: Annotated Dailies
  Export Annotated Clips...        for every clip that has annotations:
                                     <clip>_notes.mov    whole clip, annotations burned in
                                     <clip>_f<frame>.jpg still of each annotated frame
  Export Annotated Frames Only...  just the <clip>_f<frame>.jpg stills

Clips without annotations are never exported.

How it works:
  1. Walk the playlist frame by frame and record which source plays on each
     playlist frame, plus the source's own frame number.
  2. Find every RVPaint node that has strokes and match it to its clip.
  3. Save a temporary copy of the session and call rvio once per clip
     (-t playlistStart-playlistEnd) and once per annotated still.

Rendering runs in a background thread so RV stays responsive.
Log: <output folder>/annotated_export.log
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading

from rv import commands, extra_commands, rvtypes

# Qt: newer OpenRV builds ship PySide6, older ones PySide2.
try:
    from PySide6 import QtWidgets
except ImportError:  # pragma: no cover
    from PySide2 import QtWidgets


FRAME_PROP_RE = re.compile(r"\.frame:(-?\d+)\.order$")
STROKE_PROP_RE = re.compile(r"\.(?:pen|text):\d+:(-?\d+):")
SOURCE_GROUP_RE = re.compile(r"(sourceGroup\d+)")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def find_rvio():
    """Locate the rvio executable."""
    env = os.environ.get("RVIO_PATH")
    if env and os.path.isfile(env):
        return env

    exe_name = "rvio.exe" if sys.platform.startswith("win") else "rvio"
    candidates = []
    # rvio normally sits next to the rv binary.
    for base in (os.path.dirname(sys.executable),
                 os.environ.get("RV_APP_ROOT", ""),
                 os.environ.get("RV_HOME", "")):
        if base:
            candidates += [os.path.join(base, exe_name),
                           os.path.join(base, "bin", exe_name)]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return shutil.which("rvio")


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "clip"


def clip_name(source_node):
    """Readable name for a source: its media file name without extension."""
    try:
        media = commands.getStringProperty(source_node + ".media.movie")
        if media and media[0]:
            base = os.path.basename(media[0])
            # strip extension and any frame pattern: name.1001-1100#.exr -> name
            base = re.sub(r"(\.[@#%0-9-]+)?\.[A-Za-z0-9]+$", "", base)
            return safe_name(base)
    except Exception:
        pass
    return safe_name(commands.nodeGroup(source_node))


def stroke_frames(paint_node):
    """Frame numbers that have strokes on one RVPaint node."""
    frames = set()
    for prop in commands.properties(paint_node):
        m = FRAME_PROP_RE.search(prop)
        if m:
            try:
                if commands.getStringProperty(prop):
                    frames.add(int(m.group(1)))
            except Exception:
                pass
            continue
        m = STROKE_PROP_RE.search(prop)
        if m:
            frames.add(int(m.group(1)))
    return frames


def _group_source(group):
    """The file/image source node inside a source group."""
    for n in commands.nodesInGroup(group):
        if commands.nodeType(n) in ("RVFileSource", "RVImageSource"):
            return n
    return group + "_source"


def _make_segment(group, start, first_src_frame, length):
    seg = {"source": _group_source(group), "group": group,
           "start": start, "end": start + length - 1,
           "src_to_global": {}, "global_to_src": {}}
    for i in range(length):
        sf, g = first_src_frame + i, start + i
        seg["src_to_global"][sf] = g
        seg["global_to_src"][g] = sf
    return seg


def map_playlist_from_edl(sequence_group):
    """
    Read the playlist's cut list straight from the sequence node's EDL:
      edl.source  index into the sequence's inputs (last entry is a terminator)
      edl.frame   playlist frame where each cut starts
      edl.in/out  the clip's own first/last frame for that cut
    """
    seq = next((n for n in commands.nodesInGroup(sequence_group)
                if commands.nodeType(n) == "RVSequence"), None)
    if not seq:
        return []
    src = commands.getIntProperty(seq + ".edl.source")
    frm = commands.getIntProperty(seq + ".edl.frame")
    fin = commands.getIntProperty(seq + ".edl.in")
    fout = commands.getIntProperty(seq + ".edl.out")
    inputs = commands.nodeConnections(sequence_group, False)[0]

    segments = []
    for i in range(len(src) - 1):  # skip the terminator entry
        length = fout[i] - fin[i] + 1
        if length <= 0 or src[i] >= len(inputs):
            continue
        segments.append(_make_segment(inputs[src[i]], frm[i], fin[i], length))
    return segments


def map_playlist():
    """
    Returns a list of segments, one per cut in the playlist:
      {"source", "group", "start", "end", "src_to_global", "global_to_src"}
    A clip that appears twice in the playlist gives two segments.
    Uses the sequence EDL; falls back to asking RV frame by frame.
    """
    seq_group = commands.viewNode()
    if commands.nodeType(seq_group) == "RVSequenceGroup":
        try:
            segments = map_playlist_from_edl(seq_group)
            if segments:
                return segments
        except Exception as e:
            print("[annotated-dailies-export] EDL read failed, walking frames: %s" % e)

    segments = []
    current = None
    for g in range(commands.frameStart(), commands.frameEnd() + 1):
        srcs = commands.sourcesAtFrame(g)
        if not srcs:
            current = None
            continue
        src = srcs[0]
        if current is None or current["source"] != src or g != current["end"] + 1:
            current = {"source": src, "group": commands.nodeGroup(src),
                       "start": g, "end": g,
                       "src_to_global": {}, "global_to_src": {}}
            segments.append(current)
        current["end"] = g
        try:
            sf = commands.sourceFrame(g)
            current["src_to_global"][sf] = g
            current["global_to_src"][g] = sf
        except Exception:
            pass
    return segments


def collect_annotations(segments):
    """
    Set seg["annotated"] = source frames with strokes, for each segment.

    Annotations can live on:
      sourceGroup000000_paint               (drawn while viewing the clip alone)
      defaultSequence_p_sourceGroup000000   (drawn while viewing the playlist)
      a view-level paint node               (no source in its name)
    """
    for seg in segments:
        seg["annotated"] = set()

    for node in commands.nodes():
        if commands.nodeType(node) != "RVPaint":
            continue
        frames = stroke_frames(node)
        if not frames:
            continue
        m = SOURCE_GROUP_RE.search(node)
        owner = m.group(1) if m else None
        candidates = [s for s in segments if s["group"] == owner] if owner else segments

        for f in frames:
            for seg in candidates:
                if owner and f in seg["src_to_global"]:
                    seg["annotated"].add(f)
                elif seg["start"] <= f <= seg["end"]:
                    seg["annotated"].add(seg["global_to_src"].get(f, f))


# --------------------------------------------------------------------------
# Mode
# --------------------------------------------------------------------------

class AnnotatedDailiesExport(rvtypes.MinorMode):

    def __init__(self):
        rvtypes.MinorMode.__init__(self)
        self._worker = None
        self.init(
            "annotated-dailies-export",
            None,
            None,
            [("Annotated Dailies", [
                ("Export Annotated Clips...", self.export_clips, None, self.menu_state),
                ("Export Annotated Frames Only...", self.export_frames, None, self.menu_state),
            ])],
        )

    def menu_state(self):
        busy = self._worker is not None and self._worker.is_alive()
        return commands.DisabledMenuState if busy else commands.UncheckedMenuState

    # -- menu callbacks ----------------------------------------------------

    def export_clips(self, event):
        self._run(movies=True)

    def export_frames(self, event):
        self._run(movies=False)

    # -- main --------------------------------------------------------------

    def _run(self, movies):
        rvio = find_rvio()
        if not rvio:
            extra_commands.displayFeedback(
                "rvio not found - set RVIO_PATH to the rvio executable", 6.0)
            return

        out_dir = QtWidgets.QFileDialog.getExistingDirectory(
            None, "Choose output folder for annotated dailies")
        if not out_dir:
            return

        # Make sure we render the playlist, not whatever view is active.
        previous_view = commands.viewNode()
        sequence = next((n for n in commands.viewNodes()
                         if commands.nodeType(n) == "RVSequenceGroup"), None)
        if sequence:
            commands.setViewNode(sequence)

        try:
            segments = map_playlist()
            collect_annotations(segments)
            jobs = []
            used = {}
            for seg in segments:
                frames = seg["annotated"]
                if not frames:
                    continue  # never export a clip without annotations
                name = clip_name(seg["source"])
                used[name] = used.get(name, 0) + 1
                if used[name] > 1:  # same clip twice in the playlist
                    name = "%s_%d" % (name, used[name])

                if movies:
                    jobs.append({"range": "%d-%d" % (seg["start"], seg["end"]),
                                 "out": os.path.join(out_dir, name + "_notes.mov")})
                for sf in sorted(frames):
                    g = seg["src_to_global"].get(sf)
                    if g is None:
                        continue  # annotation is outside the clip's cut range
                    jobs.append({"range": "%d-%d" % (g, g),
                                 "out": os.path.join(out_dir, "%s_f%04d.jpg" % (name, sf))})

            if not jobs:
                print("[annotated-dailies-export] nothing to export. Clips seen:")
                for seg in segments:
                    print("  %s %s playlist %d-%d annotated=%s"
                          % (seg["group"], clip_name(seg["source"]),
                             seg["start"], seg["end"], sorted(seg["annotated"])))
                extra_commands.displayFeedback(
                    "No annotated clips found (details in RV's console)", 5.0)
                return

            # Snapshot the session so rendering doesn't depend on live edits.
            session_path = os.path.join(tempfile.mkdtemp(prefix="rv_annot_"),
                                        "export_session.rv")
            commands.saveSession(session_path, True, False, False)  # save as a copy
        finally:
            if previous_view and previous_view != commands.viewNode():
                commands.setViewNode(previous_view)

        extra_commands.displayFeedback(
            "Exporting %d files to %s ..." % (len(jobs), out_dir), 5.0)

        self._worker = threading.Thread(
            target=self._render, args=(rvio, session_path, jobs, out_dir), daemon=True)
        self._worker.start()

    # -- background rendering ---------------------------------------------

    @staticmethod
    def _render(rvio, session_path, jobs, out_dir):
        log_path = os.path.join(out_dir, "annotated_export.log")
        failures = 0
        with open(log_path, "w") as log:
            for i, job in enumerate(jobs, 1):
                cmd = [rvio, session_path, "-t", job["range"], "-o", job["out"], "-v"]
                log.write("[%d/%d] %s\n" % (i, len(jobs), " ".join(cmd)))
                log.flush()
                # On Windows, stop a console window flashing up for each rvio call.
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                result = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT,
                                        creationflags=flags)
                if result.returncode != 0:
                    failures += 1
                    log.write("  FAILED (exit %d)\n" % result.returncode)
            log.write("\nDone: %d jobs, %d failed\n" % (len(jobs), failures))
        try:
            os.remove(session_path)
            os.rmdir(os.path.dirname(session_path))
        except OSError:
            pass
        print("[annotated-dailies-export] finished, %d failures, log: %s"
              % (failures, log_path))


def createMode():
    return AnnotatedDailiesExport()
