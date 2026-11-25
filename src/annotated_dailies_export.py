"""
Annotated Dailies Export - an OpenRV Python mode.

For every clip in the current playlist (the default sequence view) that has
annotations, render:
  - <clip>_notes.mov        the whole clip with annotations burned in
  - <clip>_f<frame>.jpg     a still of each annotated frame

How it works:
  1. Walk the playlist frame by frame and record which source plays on each
     global frame, plus the source-relative frame number.
  2. Find each source's RVPaint node and read which source frames carry strokes.
  3. Save a temporary copy of the session and call rvio once per clip
     (-t globalStart-globalEnd) and once per annotated still.

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
            # strip sequence patterns like name.#.exr / name.1001-1100#.exr
            base = re.sub(r"[.@#%0-9-]*\.[A-Za-z0-9]+$", "", base)
            return safe_name(base)
    except Exception:
        pass
    return safe_name(commands.nodeGroup(source_node))


def annotated_source_frames(source_node):
    """Source-relative frame numbers that have paint strokes on them."""
    frames = set()
    group = commands.nodeGroup(source_node)
    for node in commands.nodesInGroup(group):
        if commands.nodeType(node) != "RVPaint":
            continue
        for prop in commands.properties(node):
            m = FRAME_PROP_RE.search(prop)
            if not m:
                continue
            try:
                order = commands.getStringProperty(prop)
            except Exception:
                order = []
            if order:  # non-empty stroke list on this frame
                frames.add(int(m.group(1)))
    return frames


def map_playlist():
    """
    Walk the playlist once. Returns a list of segments:
      {"source": node, "start": g0, "end": g1, "src_to_global": {sf: g}}
    A clip that appears twice in the playlist gives two segments.
    """
    segments = []
    current = None
    for g in range(commands.frameStart(), commands.frameEnd() + 1):
        srcs = commands.sourcesAtFrame(g)
        if not srcs:
            current = None
            continue
        src = srcs[0]
        if current is None or current["source"] != src or g != current["end"] + 1:
            current = {"source": src, "start": g, "end": g, "src_to_global": {}}
            segments.append(current)
        current["end"] = g
        try:
            sf = commands.sourceFrame(g)
            current["src_to_global"][sf] = g
        except Exception:
            pass
    return segments


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
                ("Export Annotated Clips...", self.export_annotated, None, self.menu_state),
                ("Export All Clips...", self.export_all, None, self.menu_state),
            ])],
        )

    def menu_state(self):
        busy = self._worker is not None and self._worker.is_alive()
        return commands.DisabledMenuState if busy else commands.UncheckedMenuState

    # -- menu callbacks ----------------------------------------------------

    def export_annotated(self, event):
        self._run(only_annotated=True)

    def export_all(self, event):
        self._run(only_annotated=False)

    # -- main --------------------------------------------------------------

    def _run(self, only_annotated):
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
            jobs = []
            for seg in segments:
                frames = annotated_source_frames(seg["source"])
                if only_annotated and not frames:
                    continue
                name = clip_name(seg["source"])
                # The same clip twice in a playlist gets a suffix.
                existing = [j for j in jobs if j["name"].startswith(name)]
                if any(j["kind"] == "movie" for j in existing):
                    name = "%s_%d" % (name, len(existing) + 1)

                jobs.append({"kind": "movie", "name": name,
                             "range": "%d-%d" % (seg["start"], seg["end"]),
                             "out": os.path.join(out_dir, name + "_notes.mov")})
                for sf in sorted(frames):
                    g = seg["src_to_global"].get(sf)
                    if g is None:
                        continue  # annotation is outside the clip's cut range
                    jobs.append({"kind": "still", "name": name,
                                 "range": "%d-%d" % (g, g),
                                 "out": os.path.join(out_dir, "%s_f%04d.jpg" % (name, sf))})

            if not jobs:
                extra_commands.displayFeedback("No annotated clips found", 4.0)
                return

            # Snapshot the session so rendering doesn't depend on live edits.
            session_path = os.path.join(tempfile.mkdtemp(prefix="rv_annot_"),
                                        "export_session.rv")
            commands.saveSession(session_path, True, False, False)  # save as a copy
        finally:
            if previous_view and previous_view != commands.viewNode():
                commands.setViewNode(previous_view)

        n_movies = sum(1 for j in jobs if j["kind"] == "movie")
        extra_commands.displayFeedback(
            "Exporting %d clips (%d files) to %s ..." % (n_movies, len(jobs), out_dir), 5.0)

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
        # Feedback is shown at start; check the log for completion and errors.
        print("[annotated-dailies-export] finished, %d failures, log: %s"
              % (failures, log_path))


def createMode():
    return AnnotatedDailiesExport()
