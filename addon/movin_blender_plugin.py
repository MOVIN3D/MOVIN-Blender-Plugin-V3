bl_info = {
    "name": "MOVIN Live Receiver",
    "author": "MOVIN",
    "version": (1, 1, 0),
    "blender": (4, 3, 2),
    "location": "View3D > N-Panel > MOVIN Live Receiver",
    "description": "Receives /MOVIN/Frame and /MOVIN/PointCloud OSC from Unity.",
    "category": "Animation",
}

import bpy
from bpy.props import (
    PointerProperty, StringProperty, IntProperty, BoolProperty, FloatProperty, EnumProperty
)
from bpy.types import (
    PropertyGroup, Panel, Operator
)
from pathlib import Path
import base64
import socket
import struct
import threading
import time
import traceback
from collections import deque, namedtuple
from mathutils import Quaternion, Vector
import math

# -----------------------
# OSC Reader
# -----------------------

class _OscReader:
    """Tiny OSC reader for a single message packet.
    Supports address + typetags with 'i', 'f', 's'."""
    def __init__(self, data: bytes):
        self.data = data
        self.i = 0
        self.n = len(data)

    def _read_padded_string(self):
        start = self.i
        try:
            end = self.data.index(b'\x00', start)
        except ValueError:
            raise ValueError("OSC string not null-terminated")
        s = self.data[start:end].decode('utf-8', errors='replace')
        self.i = (end + 4) & ~0x03
        if self.i > self.n:
            raise ValueError("OSC string padding overflow")
        return s

    def _read_int32(self):
        if self.i + 4 > self.n:
            raise ValueError("OSC int32 truncated")
        val = struct.unpack(">i", self.data[self.i:self.i+4])[0]
        self.i += 4
        return val

    def _read_float32(self):
        if self.i + 4 > self.n:
            raise ValueError("OSC float32 truncated")
        val = struct.unpack(">f", self.data[self.i:self.i+4])[0]
        self.i += 4
        return val

    def read_message(self):
        address = self._read_padded_string()
        if not address:
            raise ValueError("Empty OSC address")
        if self.i >= self.n:
            return address, []
        typetags = self._read_padded_string()
        if not typetags.startswith(','):
            raise ValueError("OSC typetags missing ',' prefix")
        argspec = typetags[1:]
        args = []
        for t in argspec:
            if t == 'i':
                args.append(self._read_int32())
            elif t == 'f':
                args.append(self._read_float32())
            elif t == 's':
                args.append(self._read_padded_string())
            else:
                raise ValueError(f"Unsupported OSC arg type: {t}")
        return address, args

# -----------------------------------------------------------------------------
# Skeleton Calibration Offset
#
# MOVIN Studio calibrates the streamed skeleton to the performer's body, so an
# Actor stream carries that performer's segment lengths rather than the ones
# baked into whatever armature it is driving. Nothing is lost or wrong about
# that - it is the motion data arriving intact - but the two skeletons no longer
# agree about how long a forearm is, and to anyone seeing it for the first time
# that reads as a plugin defect.
#
# Nothing here fixes anything. It detects the difference and says so, with the
# actual per-bone figures, while the user is looking at it.
#
# Character streaming needs none of this: MOVIN Studio has already retargeted
# onto the same .fbx, so the lengths match and there is nothing to explain.
#
# Everything above the "Blender bindings" divider is deliberately free of bpy so
# it can be unit tested without Blender - see tests/test_skeleton_diagnostics.py.
# -----------------------------------------------------------------------------

#: The actor name MOVIN Studio sends when streaming an Actor. Character streams
#: are named after the loaded character instead and carry no offset to report.
MOVIN_ACTOR_SUBJECT_NAME = "MOVINMan"

#: Bones must differ by more than this fraction before they are worth mentioning.
SKELETON_TOLERANCE_RATIO = 0.02

#: Frames needed before world movement can be told apart from a static length.
SKELETON_MIN_FRAMES_FOR_MOTION_CHECK = 30

#: A bone whose length changes on more than this fraction of frames is carrying
#: world movement rather than a bone length.
SKELETON_WORLD_MOTION_CHANGE_FRACTION = 0.2

#: How far a length has to move between frames to count as having changed. In
#: metres, matching the wire format - 0.5 mm, well under any real calibration
#: step and well over float noise.
SKELETON_LENGTH_CHANGE_TOLERANCE_M = 0.0005

#: Bones listed in the notice before it is truncated.
SKELETON_MAX_REPORTED_BONES = 6

#: Bone names listed per line, so the notice fits an N-panel.
SKELETON_BONES_PER_LINE = 2

#: A median streamed/rest ratio outside this range is not a calibration offset,
#: it is the two sides being measured in different units. Reporting 100x on
#: every bone would be worse than saying nothing, so the report is suppressed
#: and the reason logged instead. Real calibration spans roughly 0.5x to 1.5x,
#: so this leaves a wide margin either side.
SKELETON_PLAUSIBLE_MEDIAN_RATIO = (0.125, 8.0)

#: How often the timer compares the stream against the armature.
SKELETON_SCAN_INTERVAL_SEC = 2.0

#: A subject with no frame this recently has stopped streaming.
SKELETON_STALE_FRAME_SEC = 5.0

#: One rest-pose bone: its name, and how far its origin sits from its parent's,
#: in metres. There is deliberately no parent or depth here - see
#: find_world_motion_bones() for why the hierarchy must not be consulted.
RestBone = namedtuple("RestBone", ("name", "local_offset"))


def _display_ratio(ratio):
    """The ratio exactly as the notice prints it.

    Every comparison that decides what the user sees - ranking, the tolerance
    test, the re-notify signature - runs on this string rather than the raw
    float, so a difference too small to display can never change the outcome.
    """
    return "%.2f" % ratio


def _median(values):
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 1.0
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


class BoneLengthDeviation:
    """One bone whose streamed length differs from the armature's rest pose."""

    __slots__ = ("bone_name", "rest_length", "streamed_length", "ratio")

    def __init__(self, bone_name, rest_length, streamed_length, ratio):
        self.bone_name = bone_name
        self.rest_length = rest_length
        self.streamed_length = streamed_length
        self.ratio = ratio

    @property
    def display_ratio(self):
        return _display_ratio(self.ratio)

    @property
    def magnitude(self):
        """How far from matching, measured at display precision."""
        return abs(float(self.display_ratio) - 1.0)


class SkeletonDeviationReport:
    """Result of comparing a streamed skeleton against an armature rest pose."""

    __slots__ = ("compared_bone_count", "deviations", "median_ratio", "unit_mismatch")

    def __init__(self):
        self.compared_bone_count = 0
        self.deviations = []
        self.median_ratio = 1.0
        self.unit_mismatch = False

    def has_deviation(self):
        return bool(self.deviations) and not self.unit_mismatch


def is_actor_subject(subject_name):
    """Actor streams get diagnostics; Character streams are already retargeted."""
    return str(subject_name or "").strip().lower() == MOVIN_ACTOR_SUBJECT_NAME.lower()


def find_world_motion_bones(bone_names, length_change_counts, frames_observed):
    """Which streamed bones carry world movement rather than a bone length.

    Decided by observation, never by hierarchy shape. A calibrated bone length
    holds still for the whole session - stable to five decimal places - while
    the pelvis translation is a world position that moves on essentially every
    frame. Counting how often each length changes separates the two without
    knowing anything about the rig, which matters because the rigs disagree:
    MOVIN Studio's skeleton has a root above the pelvis and the imported .fbx
    usually does not, so any rule phrased in terms of "the root and what hangs
    off it" misses on one side or the other. Getting this wrong lets the pelvis
    into the report, and because its "length" is a position it changes on every
    scan, which makes the notice re-fire every couple of seconds.

    A recalibration changes a length once and then holds it, so it does not look
    like movement and correctly re-raises the notice instead of silencing the
    bone. A min/max range test could not tell those two apart; a change count
    can.
    """
    if frames_observed < SKELETON_MIN_FRAMES_FOR_MOTION_CHECK:
        return set()

    threshold = math.ceil(frames_observed * SKELETON_WORLD_MOTION_CHANGE_FRACTION)
    usable = min(len(bone_names), len(length_change_counts))
    return set(
        bone_names[index]
        for index in range(usable)
        if length_change_counts[index] >= threshold
    )


def compare_bone_lengths(rest_bones, streamed_bone_names, streamed_lengths,
                         excluded_bone_names=(), tolerance_ratio=SKELETON_TOLERANCE_RATIO):
    """Compare streamed bone lengths against a rest pose. Pure data in, data out.

    Both sides must already be in the same unit. Streamed values are metres;
    armature data is in Blender units and needs the object scale and the scene
    unit scale folded in first - see collect_rest_bones(). If that goes wrong
    the median ratio lands nowhere near 1.0 and the report is flagged as a unit
    mismatch rather than published as a calibration offset.

    Bones missing from either side are skipped, as are zero-length rest bones.
    Bones carrying world movement have to be excluded or their translation -
    a position, not a length - registers as an enormous deviation that changes
    every frame; the caller supplies them in excluded_bone_names.
    """
    report = SkeletonDeviationReport()

    # A short length array means a malformed frame; trust only paired indices.
    usable = min(len(streamed_bone_names), len(streamed_lengths))
    if usable == 0 or not rest_bones:
        return report

    excluded = set(excluded_bone_names)
    streamed_by_name = dict(
        (streamed_bone_names[index], streamed_lengths[index]) for index in range(usable)
    )

    ratios = []
    deviations = []
    for rest_bone in rest_bones:
        if rest_bone.name in excluded:
            continue
        streamed_length = streamed_by_name.get(rest_bone.name)
        if streamed_length is None:
            continue
        if rest_bone.local_offset <= 1e-9:
            continue

        report.compared_bone_count += 1

        deviation = BoneLengthDeviation(
            rest_bone.name,
            rest_bone.local_offset,
            streamed_length,
            streamed_length / rest_bone.local_offset,
        )
        ratios.append(deviation.ratio)
        if deviation.magnitude > tolerance_ratio:
            deviations.append(deviation)

    if ratios:
        report.median_ratio = _median(ratios)
        low, high = SKELETON_PLAUSIBLE_MEDIAN_RATIO
        report.unit_mismatch = not (low <= report.median_ratio <= high)

    deviations.sort(key=_deviation_rank_key)
    report.deviations = deviations

    return report


def rest_relative_location(streamed_offset, rest_offset, rest_axes):
    """The pose_bone.location that puts a bone's origin where MOVIN Studio says.

    Everything is in armature units, in the PARENT bone's rest frame - the frame
    Blender's pose evaluation works in, and the frame Unity's localPosition is
    already expressed in:

      * streamed_offset - the calibrated offset of this bone from its parent
      * rest_offset     - the armature's own offset, L_t below
      * rest_axes       - the columns of L_R below

    Blender evaluates a bone as

        pose = parent_pose @ (parent_rest^-1 @ rest) @ basis

    so with L = parent_rest^-1 @ rest, the origin lands at L_t + L_R @ t in the
    parent's frame, where t is the location channel. Solving for t gives
    t = L_R^-1 @ (p - L_t). L_R is orthonormal, so the inverse is a projection
    onto its columns - which is all this function does.

    Getting the frame wrong is not a small error. Using the bone's own axes and an
    armature-space offset instead computes a different quantity that only looks
    right where a bone happens to sit near the armature axes. It does not:
    MOVINman's bones average 12 degrees off with thumbs at 60, and Ch14's average
    116 degrees - which showed up first as mangled fingers.

    Referencing the rest pose is also what makes this deterministic. Taking the
    first arriving frame as the baseline made the result depend on whichever frame
    landed first - one from before calibration finished, or during a
    recalibration, threw off everything after it, which is the only reason a
    manual "reset the reference" action ever had to exist.

    Returns (0, 0, 0) when the stream matches the rest pose, which is what a
    Character stream retargeted onto this same .fbx produces.
    """
    dx = streamed_offset[0] - rest_offset[0]
    dy = streamed_offset[1] - rest_offset[1]
    dz = streamed_offset[2] - rest_offset[2]
    return (
        rest_axes[0][0] * dx + rest_axes[0][1] * dy + rest_axes[0][2] * dz,
        rest_axes[1][0] * dx + rest_axes[1][1] * dy + rest_axes[1][2] * dz,
        rest_axes[2][0] * dx + rest_axes[2][1] * dy + rest_axes[2][2] * dz,
    )


def armature_units_per_metre(object_scale_xyz, scale_length):
    """How many armature units make up one streamed metre.

    MOVIN Studio streams metres; bone data lives in the armature's own units.
    The two are related by the armature object's scale and Scene > Units > Unit
    Scale, so this factor is derivable rather than something to dial in by hand:

      * MOVINman_V3_Sample - object scale 1.0, bones in metres -> 1.0
      * Ch14_Sample        - object scale 0.01, bones 100x larger -> 100.0

    The old fixed x100 was the second case hard-coded, which overshot a
    metre-scale rig by a hundred times and pulled its joints apart.

    Non-uniform scale has no single answer, so the mean is used; a rig scaled
    unevenly cannot have its bone offsets reproduced faithfully either way.
    """
    mean_scale = (abs(object_scale_xyz[0]) + abs(object_scale_xyz[1]) + abs(object_scale_xyz[2])) / 3.0
    metres_per_unit = mean_scale * scale_length
    if metres_per_unit <= 1e-12:
        return 1.0
    return 1.0 / metres_per_unit


def _deviation_rank_key(deviation):
    """Ranking key for the printed list: worst first, ties broken by name.

    The magnitude is quantised to the two decimals the notice prints before it is
    compared, so two bones the user cannot distinguish are a genuine tie and fall
    through to the name. Ranking on the raw float instead is what let a
    performer's near-identical left and right sides swap places between scans -
    same bones, same printed ratios, different order - which was enough to look
    like a new report and re-raise the notice every two seconds.
    """
    return (-int(round(deviation.magnitude * 100)), deviation.bone_name)


def build_report_signature(target_name, report):
    """Identity of what a report would put on screen.

    Built from the displayed figures rather than the raw stream, so neither a
    pelvis that moves every frame nor jitter below display precision reads as a
    fresh recalibration. Sorted by name rather than by rank, because the
    signature answers "is this the same set of figures" and the order the notice
    happens to list them in is not part of that.
    """
    if report.unit_mismatch:
        return "%s|unit-mismatch:%s" % (target_name, _display_ratio(report.median_ratio))
    if not report.deviations:
        return "%s|match" % target_name

    entries = sorted(
        "%s=%s" % (deviation.bone_name, deviation.display_ratio)
        for deviation in report.deviations
    )
    return target_name + "|" + "|".join(entries)


def format_report_lines(subject_name, target_name, report):
    """The user-facing notice, pre-split into panel-width lines.

    The whole feature rests on this text. Naming the bone the user is squinting
    at, with its actual ratio, is what turns "the plugin is broken" into "the
    plugin already knows about this, so it is meant to happen". It says outright
    that this is not an error, and it points at the paths that do produce a
    finished character instead of offering a fix that cannot exist.
    """
    shown = report.deviations[:SKELETON_MAX_REPORTED_BONES]
    entries = ["%s %sx" % (deviation.bone_name, deviation.display_ratio) for deviation in shown]
    remaining = len(report.deviations) - len(shown)
    if remaining > 0:
        entries.append("(+%d more)" % remaining)

    bone_lines = [
        "    " + ", ".join(entries[index:index + SKELETON_BONES_PER_LINE])
        for index in range(0, len(entries), SKELETON_BONES_PER_LINE)
    ]

    return [
        "Subject '%s' is streaming bone lengths calibrated to the" % subject_name,
        "performer's body. Armature '%s' was built to different" % target_name,
        "proportions, so the two skeletons differ at these bones",
        "(streamed / rest length, largest first):",
    ] + bone_lines + [
        "The mesh visibly changes shape at those joints. This is",
        "expected, not a plugin error - it means the motion data is",
        "being applied without loss.",
        "For a finished character, stream a Character from MOVIN",
        "Studio, or retarget this take onto your own rig.",
    ]


def format_report(subject_name, target_name, report):
    return "\n".join(format_report_lines(subject_name, target_name, report))


# -----------------------
# Runtime
# -----------------------

class MOVINRuntime:
    def __init__(self):
        self.thread = None
        self.sock = None
        self.running = False
        self.lock = threading.Lock()
        self.frame_buffers = {}
        self.pointcloud_buffers = {}
        self.ready_frames = deque(maxlen=4)
        self.ready_pointclouds = deque(maxlen=2)
        # Rest data from collect_bone_rest_frames(), and the armature it came
        # from. Rebuilt when the bound armature changes. Nothing here is a
        # baseline captured from the stream - the rest pose is the reference.
        self.bone_rest_frames = {}
        self.bone_rest_frames_armature = ""
        self.warned_connected_bones = False
        self.last_applied = None
        self.last_pointcloud_frame = None
        self.last_actor = ""
        self.last_ts = ""
        self.last_pointcloud_count = 0
        self.last_visualized_point_count = 0
        self.received_packets = 0
        self.frame_packets = 0
        self.completed_frames = 0
        self.last_packet_time = ""
        self.last_packet_address = ""
        self.last_packet_bytes = 0
        self.last_sender = ""
        self.last_parse_error = ""
        self.socket_error = ""
        self.recv_count = 0
        self.last_rate_time = time.time()
        self.recv_rate_hz = 0.0
        self.socket_poll_count = 0
        self.last_socket_poll_rate_time = time.time()
        self.socket_poll_rate_hz = 0.0
        self.completed_frame_rate_count = 0
        self.last_completed_frame_rate_time = time.time()
        self.completed_frame_rate_hz = 0.0
        self.warned_constraints = False  # NEW: one-time console warning
        self._reset_skeleton_diagnostics()

    def _reset_skeleton_diagnostics(self):
        self.skeleton_subject = ""
        self.skeleton_bone_names = []
        self.skeleton_lengths = []
        self.skeleton_previous_lengths = []
        self.skeleton_length_change_counts = []
        self.skeleton_frames_observed = 0
        self.skeleton_last_frame_time = 0.0
        self.skeleton_last_scan_time = 0.0

        # Signature of the notice currently raised, keyed by (subject, armature)
        # rather than held as a single value per subject: one subject can drive
        # more than one armature over a session, and a single slot makes the two
        # overwrite each other's record and re-notify on every scan.
        self.skeleton_notified_signatures = {}

        # The notice the panel is drawing, or None. Cleared by Dismiss.
        self.skeleton_banner = None

    def note_skeleton_locked(self, subject_name, bones):
        """Record the skeleton carried by one completed frame.

        Called from the receiver thread with self.lock already held, so it sees
        every frame - the timer only ever applies the newest one and would
        undercount. Keeps the latest lengths for the main thread to compare, and
        counts how often each length changes so world movement can be told apart
        from a bone length later.

        Streamed positions are metres (MOVIN Studio's wire format for OSC), and
        a vector length is unaffected by the axis flips in
        unity_to_blender_vec(), so the raw values are used as they arrive.
        """
        if not bones or not is_actor_subject(subject_name):
            return

        self.skeleton_subject = str(subject_name)

        # A different bone list is a different skeleton, and the movement counts
        # collected for the old one mean nothing for the new one.
        bone_names = [bone["bone_name"] for bone in bones]
        if bone_names != self.skeleton_bone_names:
            self.skeleton_bone_names = bone_names
            self.skeleton_length_change_counts = [0] * len(bone_names)
            self.skeleton_previous_lengths = [0.0] * len(bone_names)
            self.skeleton_frames_observed = 0
            self.skeleton_notified_signatures.clear()
            self.skeleton_banner = None

        is_first_frame = self.skeleton_frames_observed == 0
        lengths = []
        for index, bone in enumerate(bones):
            px, py, pz = bone["p"]
            length = math.sqrt(px * px + py * py + pz * pz)
            lengths.append(length)
            if (not is_first_frame
                    and abs(length - self.skeleton_previous_lengths[index]) > SKELETON_LENGTH_CHANGE_TOLERANCE_M):
                self.skeleton_length_change_counts[index] += 1
            self.skeleton_previous_lengths[index] = length

        self.skeleton_lengths = lengths
        self.skeleton_frames_observed += 1
        self.skeleton_last_frame_time = time.time()

    def reset(self):
        with self.lock:
            self.frame_buffers.clear()
            self.pointcloud_buffers.clear()
            self.ready_frames.clear()
            self.ready_pointclouds.clear()
            self.bone_rest_frames.clear()
            self.bone_rest_frames_armature = ""
            self.warned_connected_bones = False
            self.last_applied = None
            self.last_pointcloud_frame = None
            self.last_actor = ""
            self.last_ts = ""
            self.last_pointcloud_count = 0
            self.last_visualized_point_count = 0
            self.received_packets = 0
            self.frame_packets = 0
            self.completed_frames = 0
            self.last_packet_time = ""
            self.last_packet_address = ""
            self.last_packet_bytes = 0
            self.last_sender = ""
            self.last_parse_error = ""
            self.socket_error = ""
            self.recv_count = 0
            self.recv_rate_hz = 0.0
            self.socket_poll_count = 0
            self.last_socket_poll_rate_time = time.time()
            self.socket_poll_rate_hz = 0.0
            self.completed_frame_rate_count = 0
            self.last_completed_frame_rate_time = time.time()
            self.completed_frame_rate_hz = 0.0
            self.warned_constraints = False
            self._reset_skeleton_diagnostics()

_runtime = MOVINRuntime()

# -----------------------
# Stream Validation Logger
# -----------------------

class _StreamValidationLogger:
    TARGET_NAME = "Blender"
    PACKET_HEADER = "MOVIN_STREAM_VALIDATION_PACKET_V1"
    POSE_HEADER = "MOVIN_STREAM_VALIDATION_POSE_V1"
    PACKET_FORMAT = "base64_udp_datagram"
    FLOAT_FORMAT = "round6"

    def __init__(self):
        self.lock = threading.Lock()
        self.session_id = ""
        self.target = self.TARGET_NAME
        self.directory = None
        self.packet_path = None
        self.pose_path = None
        self.packet_idx = 0
        self.running = False

    def begin(self, session_id, target, duration_seconds, directory_path, port):
        with self.lock:
            try:
                self._reset()
                self.session_id = str(session_id)
                self.target = str(target or self.TARGET_NAME)
                self.directory = Path(directory_path)
                self.packet_path = self.directory / f"{self.session_id}_Plugin"
                self.pose_path = self.directory / f"{self.session_id}_PluginApplied"
                self.packet_idx = 0
                self.directory.mkdir(parents=True, exist_ok=True)
                self._write_packet_header()
                self._write_pose_header()
                self.running = True
                print(f"[MOVIN Live] Stream validation started: {self.session_id} ({self.target}) on UDP {int(port)}")
            except Exception as e:
                print("[MOVIN Live] Stream validation begin error:", e)
                self._reset()

    def end(self, session_id=""):
        with self.lock:
            if self.running and (not session_id or str(session_id) == self.session_id):
                print(f"[MOVIN Live] Stream validation ended: {self.session_id}")
                self._reset()

    def close(self):
        with self.lock:
            self._reset()

    def log_packet(self, data, frame_idx):
        if int(frame_idx) >= 0:
            return

        with self.lock:
            if not self.running or self.packet_path is None:
                return

            try:
                with self.packet_path.open("a", encoding="utf-8", newline="\n") as f:
                    f.write(f"{self.packet_idx:06d}|{base64.b64encode(data).decode('ascii')}\n")
                self.packet_idx += 1
            except Exception as e:
                print("[MOVIN Live] Stream validation packet log error:", e)
                self._reset()

    def log_pose_frame(self, frame):
        frame_idx = int(frame.get("frame_idx", 0))
        if frame_idx >= 0:
            return

        with self.lock:
            if not self.running or self.pose_path is None:
                return

            try:
                with self.pose_path.open("a", encoding="utf-8", newline="\n") as f:
                    for bone in frame["bones"]:
                        f.write(self._pose_line(frame_idx, bone) + "\n")
            except Exception as e:
                print("[MOVIN Live] Stream validation pose log error:", e)
                self._reset()

    def _write_packet_header(self):
        with self.packet_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(f"{self.PACKET_HEADER}\n")
            f.write(f"session={self.session_id}\n")
            f.write(f"target={self.target}\n")
            f.write(f"packet_format={self.PACKET_FORMAT}\n")

    def _write_pose_header(self):
        with self.pose_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(f"{self.POSE_HEADER}\n")
            f.write(f"session={self.session_id}\n")
            f.write(f"target={self.target}\n")
            f.write(f"float={self.FLOAT_FORMAT}\n")

    def _reset(self):
        self.session_id = ""
        self.target = self.TARGET_NAME
        self.directory = None
        self.packet_path = None
        self.pose_path = None
        self.packet_idx = 0
        self.running = False

    def _pose_line(self, frame_idx, bone):
        q = bone["q"]
        p = bone["p"]
        s = bone["s"]
        return "|".join((
            str(int(frame_idx)),
            str(bone["bone_name"]),
            self._fmt(p[0]),
            self._fmt(p[1]),
            self._fmt(p[2]),
            self._fmt(q[1]),
            self._fmt(q[2]),
            self._fmt(q[3]),
            self._fmt(q[0]),
            self._fmt(s[0]),
            self._fmt(s[1]),
            self._fmt(s[2]),
        ))

    def _fmt(self, value):
        v = float(value)
        rounded = math.copysign(math.floor(abs(v) * 1000000.0 + 0.5) / 1000000.0, v)
        if rounded == 0.0:
            rounded = 0.0
        return f"{rounded:.6f}"

_validation_logger = _StreamValidationLogger()

# -----------------------
# Scene Properties
# -----------------------

class MOVIN_Props(PropertyGroup):
    armature_name: StringProperty(
        name="Armature",
        description="Armature object to drive",
        default=""
    )
    port: IntProperty(
        name="Port",
        default=11235,
        min=1, max=65535
    )
    hips_bone_name: StringProperty(
        name="Hips Bone",
        description="Bone that should receive the global translation/rotation when Root is not applied to object",
        default="Hips"
    )
    hips_y_offset: FloatProperty(
        name="Performer Hips Height (m)",
        description=(
            "The performer's standing hips height in metres, negated. Streamed hips "
            "height is measured against this, so only the movement away from standing "
            "is transferred and the armature keeps its own hips height. The scale "
            "itself is derived from the armature, not set here"
        ),
        default=-0.87
    )
    is_running: BoolProperty(
        name="Running",
        default=False
    )
    pointcloud_enabled: BoolProperty(
        name="Visualize Point Cloud",
        description="Receive /MOVIN/PointCloud and update a Blender point cloud object",
        default=True
    )
    pointcloud_object_name: StringProperty(
        name="Point Cloud Object",
        description="Object name used for the live point cloud visualization",
        default="MOVIN_PointCloud"
    )
# -----------------------
# OSC Server Thread
# -----------------------

def _udp_server_loop(port):
    global _runtime
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    except OSError:
        pass

    try:
        sock.bind(("0.0.0.0", port))
        sock.settimeout(1.0 / 120.0)
        _runtime.sock = sock
        with _runtime.lock:
            _runtime.socket_error = ""
    except OSError as e:
        with _runtime.lock:
            _runtime.socket_error = f"Failed to bind UDP {port}: {e}"
        _runtime.running = False
        try:
            sock.close()
        except Exception:
            pass
        print("[MOVIN Live]", _runtime.socket_error)
        return

    PARTIAL_TTL_SEC = 0.5

    try:
        while _runtime.running:
            now = time.time()
            with _runtime.lock:
                _runtime.socket_poll_count += 1
                socket_dt = now - _runtime.last_socket_poll_rate_time
                if socket_dt >= 1.0:
                    _runtime.socket_poll_rate_hz = _runtime.socket_poll_count / socket_dt
                    _runtime.socket_poll_count = 0
                    _runtime.last_socket_poll_rate_time = now

                recv_dt = now - _runtime.last_rate_time
                if recv_dt >= 1.0:
                    _runtime.recv_rate_hz = _runtime.recv_count / recv_dt
                    _runtime.recv_count = 0
                    _runtime.last_rate_time = now

                frame_dt = now - _runtime.last_completed_frame_rate_time
                if frame_dt >= 1.0:
                    _runtime.completed_frame_rate_hz = _runtime.completed_frame_rate_count / frame_dt
                    _runtime.completed_frame_rate_count = 0
                    _runtime.last_completed_frame_rate_time = now

            try:
                data, addr = sock.recvfrom(65535)
                with _runtime.lock:
                    _runtime.received_packets += 1
                    _runtime.last_packet_time = time.strftime("%H:%M:%S")
                    _runtime.last_packet_bytes = len(data)
                    _runtime.last_sender = f"{addr[0]}:{addr[1]}"
            except socket.timeout:
                now = time.time()
                with _runtime.lock:
                    stale_frames = [k for k, v in _runtime.frame_buffers.items()
                                    if now - v.get("_t0", now) > PARTIAL_TTL_SEC]
                    stale_pointclouds = [k for k, v in _runtime.pointcloud_buffers.items()
                                         if now - v.get("_t0", now) > PARTIAL_TTL_SEC]
                    for k in stale_frames:
                        del _runtime.frame_buffers[k]
                    for k in stale_pointclouds:
                        del _runtime.pointcloud_buffers[k]
                continue
            except OSError:
                break

            try:
                reader = _OscReader(data)
                address, args = reader.read_message()
            except Exception as e:
                print("[MOVIN Live] OSC parse error:", e)
                with _runtime.lock:
                    _runtime.last_parse_error = str(e)
                continue

            now = time.time()
            with _runtime.lock:
                _runtime.last_packet_address = address
                _runtime.last_parse_error = ""
                _runtime.recv_count += 1

            if address == "/MOVIN/StreamValidation/Begin":
                try:
                    session_id = args[0]
                    target = args[1]
                    duration_seconds = int(args[2])
                    directory_path = args[3]
                except Exception as e:
                    print("[MOVIN Live] Bad validation begin args:", e)
                    with _runtime.lock:
                        _runtime.last_parse_error = f"Bad validation begin args: {e}"
                    continue

                _validation_logger.begin(session_id, target, duration_seconds, directory_path, port)
                continue

            if address == "/MOVIN/StreamValidation/End":
                try:
                    session_id = args[0]
                except Exception:
                    session_id = ""

                _validation_logger.end(session_id)
                continue

            if address == "/MOVIN/Frame":
                try:
                    ts = args[0]
                    actor_name = args[1]
                    frame_idx = int(args[2])
                    num_chunks = int(args[3])
                    chunk_idx = int(args[4])
                    total_bones = int(args[5])
                    chunk_bones = int(args[6])
                except Exception as e:
                    print("[MOVIN Live] Bad frame header args:", e)
                    with _runtime.lock:
                        _runtime.last_parse_error = f"Bad frame header args: {e}"
                    continue

                _validation_logger.log_packet(data, frame_idx)

                k = 7
                bones_in_chunk = []
                try:
                    for _ in range(chunk_bones):
                        bone_index = int(args[k]); k += 1
                        parent_index = int(args[k]); k += 1
                        bone_name = args[k]; k += 1
                        px = float(args[k]); py = float(args[k+1]); pz = float(args[k+2]); k += 3
                        rqx = float(args[k]); rqy = float(args[k+1]); rqz = float(args[k+2]); rqw = float(args[k+3]); k += 4
                        qx = float(args[k]); qy = float(args[k+1]); qz = float(args[k+2]); qw = float(args[k+3]); k += 4
                        sx = float(args[k]); sy = float(args[k+1]); sz = float(args[k+2]); k += 3
                        bones_in_chunk.append({
                            "bone_index": bone_index,
                            "parent_index": parent_index,
                            "bone_name": bone_name,
                            "p": (px, py, pz),
                            "rq": (rqw, rqx, rqy, rqz),
                            "q": (qw, qx, qy, qz),  # (w,x,y,z)
                            "s": (sx, sy, sz),
                        })
                except Exception as e:
                    print("[MOVIN Live] Truncated/invalid bone block:", e)
                    with _runtime.lock:
                        _runtime.last_parse_error = f"Truncated/invalid bone block: {e}"
                    continue

                key = (actor_name, frame_idx)
                frame_to_log = None
                with _runtime.lock:
                    _runtime.frame_packets += 1
                    buf = _runtime.frame_buffers.get(key)
                    if buf is None:
                        buf = {
                            "_t0": now,
                            "timestamp": ts,
                            "actor": actor_name,
                            "frame_idx": frame_idx,
                            "num_chunks": num_chunks,
                            "total_bones": total_bones,
                            "chunks": {},
                        }
                        _runtime.frame_buffers[key] = buf

                    buf["chunks"][chunk_idx] = bones_in_chunk

                    if len(buf["chunks"]) >= buf["num_chunks"]:
                        ordered = []
                        complete = True
                        for ci in range(buf["num_chunks"]):
                            part = buf["chunks"].get(ci)
                            if not part:
                                complete = False
                                break
                            ordered.extend(part)
                        if complete and ordered:
                            frame = {
                                "timestamp": buf["timestamp"],
                                "actor": buf["actor"],
                                "frame_idx": buf["frame_idx"],
                                "bones": ordered,
                            }
                            _runtime.ready_frames.append(frame)
                            _runtime.last_actor = buf["actor"]
                            _runtime.last_ts = buf["timestamp"]
                            frame_to_log = frame
                            _runtime.completed_frames += 1
                            _runtime.completed_frame_rate_count += 1
                            _runtime.note_skeleton_locked(buf["actor"], ordered)
                        del _runtime.frame_buffers[key]

                if frame_to_log is not None:
                    _validation_logger.log_pose_frame(frame_to_log)

            elif address == "/MOVIN/PointCloud":
                try:
                    frame_idx = int(args[0])
                    total_points = int(args[1])
                    chunk_idx = int(args[2])
                    num_chunks = int(args[3])
                    chunk_point_count = int(args[4])
                except Exception as e:
                    print("[MOVIN Live] Bad point cloud header args:", e)
                    with _runtime.lock:
                        _runtime.last_parse_error = f"Bad point cloud header args: {e}"
                    continue

                k = 5
                points_in_chunk = []
                try:
                    for _ in range(chunk_point_count):
                        px = float(args[k]); py = float(args[k+1]); pz = float(args[k+2]); k += 3
                        points_in_chunk.append((px, py, pz))
                except Exception as e:
                    print("[MOVIN Live] Truncated/invalid point cloud block:", e)
                    with _runtime.lock:
                        _runtime.last_parse_error = f"Truncated/invalid point cloud block: {e}"
                    continue

                key = frame_idx
                with _runtime.lock:
                    buf = _runtime.pointcloud_buffers.get(key)
                    if buf is None:
                        buf = {
                            "_t0": now,
                            "frame_idx": frame_idx,
                            "num_chunks": num_chunks,
                            "total_points": total_points,
                            "chunks": {},
                        }
                        _runtime.pointcloud_buffers[key] = buf

                    buf["chunks"][chunk_idx] = points_in_chunk

                    if len(buf["chunks"]) >= buf["num_chunks"]:
                        ordered = []
                        complete = True
                        for ci in range(buf["num_chunks"]):
                            part = buf["chunks"].get(ci)
                            if part is None:
                                complete = False
                                break
                            ordered.extend(part)
                        if complete:
                            pointcloud = {
                                "frame_idx": buf["frame_idx"],
                                "points": ordered,
                            }
                            _runtime.ready_pointclouds.append(pointcloud)
                            _runtime.last_pointcloud_frame = buf["frame_idx"]
                            _runtime.last_pointcloud_count = len(ordered)
                        del _runtime.pointcloud_buffers[key]

            else:
                continue

    finally:
        try:
            sock.close()
        except Exception:
            pass
        _validation_logger.close()
        _runtime.sock = None

# -----------------------
# Coordinate & Math
# -----------------------

def unity_to_blender_vec(v):
    return (-v[0], v[1], v[2])

def unity_to_blender_pointcloud_vec(v):
    return (-v[0], -v[2], v[1])

def unity_to_blender_quat(q):
    return (q[0], q[1], -q[2], -q[3])

def rotate_vec(v, q):
    """
    Rotate vector v by quaternion q.
    v: (x,y,z) tuple/list
    q: (w,x,y,z) tuple/list
    returns (x,y,z) tuple
    """
    quat = Quaternion((q[0], q[1], q[2], q[3]))  # one iterable!
    vec = Vector(v)
    vec.rotate(quat)  # in-place rotation
    return (vec.x, vec.y, vec.z)

def quat_mul(q1, q2):
    w1,x1,y1,z1 = q1; w2,x2,y2,z2 = q2
    return (
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2
    )

def quat_conj(q):
    w,x,y,z = q
    return (w, -x, -y, -z)

def quat_from_euler(euler):
    x, y, z = euler
    x = math.radians(x)
    y = math.radians(y)
    z = math.radians(z)
    return (
        math.cos(x/2) * math.cos(y/2) * math.cos(z/2) + math.sin(x/2) * math.sin(y/2) * math.sin(z/2),
        math.sin(x/2) * math.cos(y/2) * math.cos(z/2) - math.cos(x/2) * math.sin(y/2) * math.sin(z/2),
        math.cos(x/2) * math.sin(y/2) * math.cos(z/2) + math.sin(x/2) * math.cos(y/2) * math.sin(z/2),
        math.cos(x/2) * math.cos(y/2) * math.sin(z/2) - math.sin(x/2) * math.sin(y/2) * math.cos(z/2)
    )

def _ensure_pointcloud_gn_tree():
    group_name = "MOVIN_PointCloud_GN"
    node_group = bpy.data.node_groups.get(group_name)
    if node_group is not None:
        return node_group

    node_group = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')

    interface = node_group.interface
    interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    interface.new_socket(name="Radius", in_out='INPUT', socket_type='NodeSocketFloat')

    nodes = node_group.nodes
    links = node_group.links
    nodes.clear()

    group_input = nodes.new("NodeGroupInput")
    group_input.location = (-400, 0)

    mesh_to_points = nodes.new("GeometryNodeMeshToPoints")
    mesh_to_points.location = (-100, 0)
    mesh_to_points.mode = 'VERTICES'

    set_radius = nodes.new("GeometryNodeSetPointRadius")
    set_radius.location = (150, 0)

    group_output = nodes.new("NodeGroupOutput")
    group_output.location = (400, 0)

    links.new(group_input.outputs["Geometry"], mesh_to_points.inputs["Mesh"])
    links.new(mesh_to_points.outputs["Points"], set_radius.inputs["Points"])
    links.new(group_input.outputs["Radius"], set_radius.inputs["Radius"])
    links.new(set_radius.outputs["Points"], group_output.inputs["Geometry"])

    return node_group

def _ensure_pointcloud_modifier(obj):
    modifier = obj.modifiers.get("MOVIN_PointCloud")
    if modifier is None:
        modifier = obj.modifiers.new(name="MOVIN_PointCloud", type='NODES')
    modifier.node_group = _ensure_pointcloud_gn_tree()
    return modifier

def _ensure_pointcloud_material(material_name, rgba):
    mat = bpy.data.materials.get(material_name)
    if mat is None:
        mat = bpy.data.materials.new(material_name)
        mat.use_nodes = True

    if mat.use_nodes:
        nodes = mat.node_tree.nodes
        principled = nodes.get("Principled BSDF")
        if principled is not None:
            principled.inputs["Base Color"].default_value = rgba
            principled.inputs["Emission Color"].default_value = rgba
            principled.inputs["Emission Strength"].default_value = 1.0
            principled.inputs["Roughness"].default_value = 0.35
    mat.diffuse_color = rgba
    return mat

def _ensure_pointcloud_object(scene, object_name):
    obj = bpy.data.objects.get(object_name)
    if obj is not None and obj.type == 'MESH':
        _ensure_pointcloud_modifier(obj)
        return obj

    if obj is not None and obj.type != 'MESH':
        print(f"[MOVIN Live] Existing object '{object_name}' is not a Mesh")
        return None

    mesh = bpy.data.meshes.new(object_name)
    obj = bpy.data.objects.new(object_name, mesh)
    _ensure_pointcloud_modifier(obj)
    obj.display_type = 'WIRE'
    obj.hide_select = True
    obj.show_wire = True

    scene.collection.objects.link(obj)
    return obj

def _update_pointcloud_object(scene, object_name, points, radius, color_rgba):
    obj = _ensure_pointcloud_object(scene, object_name)
    if obj is None:
        return

    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata(points, [], [])
    mesh.update()

    modifier = _ensure_pointcloud_modifier(obj)
    try:
        modifier["Socket_2"] = radius
    except Exception:
        pass

    material = _ensure_pointcloud_material(object_name + "_MAT", color_rgba)
    if len(mesh.materials) == 0:
        mesh.materials.append(material)
    else:
        mesh.materials[0] = material
    obj.color = color_rgba

def _downsample_points(points, max_points):
    if max_points <= 0 or len(points) <= max_points:
        return points
    step = max(1, math.ceil(len(points) / max_points))
    return points[::step][:max_points]

# -----------------------------------------------------------------------------
# Skeleton Calibration Offset - Blender bindings (main thread only)
# -----------------------------------------------------------------------------

def collect_rest_bones(arm_obj, scale_length=1.0):
    """Rest-pose bone offsets for one armature, in metres.

    Two conversions have to happen here or the comparison is meaningless, and
    this is the one place in the feature where Blender differs materially from
    Unreal. Bone data is stored in the armature's own space in Blender units,
    while the stream is in metres:

      * the armature object's scale has to be folded in. MOVINman_V3_Sample sits
        at scale 1.0 with metre-sized bones, but Ch14_Sample's armature is at
        0.01 with bone data 100x larger, which is what an .fbx import from a
        centimetre-based DCC leaves behind. Going through matrix_world covers
        that, and non-uniform scale and parenting with it.
      * Scene > Units > Unit Scale maps Blender units to metres on top of that.

    Mistaking either factor for a calibration offset would report every bone at
    100x, so compare_bone_lengths() re-checks the result and refuses to publish
    a report whose median lands nowhere near 1.0.

    A bone with no parent is skipped: it has no parent-relative offset, so there
    is nothing to compare. That is not the hierarchy-based exclusion warned about
    in find_world_motion_bones() - a pelvis parented to a root bone still gets
    compared here, and it is the change counting that keeps it out of the report.
    """
    matrix = arm_obj.matrix_world
    rest_bones = []
    for bone in arm_obj.data.bones:
        parent = bone.parent
        if parent is None:
            continue
        offset = (matrix @ bone.head_local) - (matrix @ parent.head_local)
        rest_bones.append(RestBone(bone.name, offset.length * scale_length))
    return rest_bones


def collect_bone_rest_frames(arm_obj):
    """The rest offset and axes rest_relative_location() needs, for every bone.

    L = parent_rest^-1 @ rest, per that function's derivation: the offset is L_t
    and the axes are the columns of L_R, both in armature units in the parent
    bone's rest frame. Pairs with armature_units_per_metre(), which brings the
    streamed metres into the same units. Not to be confused with
    collect_rest_bones(), which normalises to metres in armature space for the
    calibration report.

    Built once when streaming starts rather than per frame - it is fixed rest
    data. Editing the armature mid-session means pressing Stop and Start.

    A parentless bone has no parent-relative offset; only its axes are recorded,
    for the hips, whose translation is a world position handled separately.
    """
    frames = {}
    for bone in arm_obj.data.bones:
        parent = bone.parent
        if parent is None:
            frames[bone.name] = {
                "offset": None,
                "axes": tuple(tuple(axis) for axis in bone.matrix_local.to_3x3().transposed()),
                "connected": False,
            }
            continue

        relative = parent.matrix_local.inverted() @ bone.matrix_local
        offset = relative.to_translation()
        frames[bone.name] = {
            "offset": (offset.x, offset.y, offset.z),
            "axes": tuple(tuple(axis) for axis in relative.to_3x3().transposed()),
            # Blender locks the location channel of a connected bone, so an offset
            # written to one goes nowhere.
            "connected": bone.use_connect,
        }
    return frames


def _tag_skeleton_notice_redraw():
    """Repaint the N-panel so the notice appears without the user poking at it."""
    try:
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    except Exception:
        pass


def _scan_skeleton_calibration(scene, props):
    """Compare the streamed skeleton against the bound armature, and say so once.

    Runs on the timer because it reads bpy data. Rate limited, and held off until
    enough frames have been seen to tell a moving pelvis apart from a static bone
    length - before that, nothing is shown at all.

    Only the armature named in the panel is examined. Unreal had to work out
    which Skeletal Mesh a subject actually drove, because bone names alone match
    every Mixamo-derived rig in the level; in Blender the binding is explicit, so
    that whole problem does not arise.
    """
    now = time.time()

    with _runtime.lock:
        if _runtime.skeleton_frames_observed < SKELETON_MIN_FRAMES_FOR_MOTION_CHECK:
            return
        if (now - _runtime.skeleton_last_frame_time) > SKELETON_STALE_FRAME_SEC:
            return
        if (now - _runtime.skeleton_last_scan_time) < SKELETON_SCAN_INTERVAL_SEC:
            return
        _runtime.skeleton_last_scan_time = now

        subject = _runtime.skeleton_subject
        bone_names = list(_runtime.skeleton_bone_names)
        streamed_lengths = list(_runtime.skeleton_lengths)
        change_counts = list(_runtime.skeleton_length_change_counts)
        frames_observed = _runtime.skeleton_frames_observed

    arm_obj = bpy.data.objects.get(props.armature_name) if props.armature_name else None
    if arm_obj is None or arm_obj.type != 'ARMATURE':
        return

    rest_bones = collect_rest_bones(arm_obj, scene.unit_settings.scale_length)
    world_motion_bones = find_world_motion_bones(bone_names, change_counts, frames_observed)
    report = compare_bone_lengths(rest_bones, bone_names, streamed_lengths, world_motion_bones)

    target_name = arm_obj.name
    signature = build_report_signature(target_name, report)
    notice_key = (subject, target_name)

    # Say it once per armature. The scan repeats for as long as the subject
    # streams, so this is what keeps a standing offset from re-raising the notice
    # every couple of seconds - and what makes a real recalibration raise it
    # again.
    lines = format_report_lines(subject, target_name, report) if report.has_deviation() else None
    with _runtime.lock:
        if _runtime.skeleton_notified_signatures.get(notice_key) == signature:
            return
        _runtime.skeleton_notified_signatures[notice_key] = signature
        _runtime.skeleton_banner = None if lines is None else {
            "subject": subject,
            "target": target_name,
            "lines": lines,
        }

    # If this notice ever starts repeating again, this is the line that says why:
    # a bone carrying world movement that was not filtered out shows up in the
    # signature and changes on every scan.
    print("[MOVIN Live] Skeleton diagnostics: subject='%s' armature='%s' signature='%s' "
          "(world-motion bones excluded: %s; frames observed: %d; bones compared: %d; "
          "median ratio %s)"
          % (subject, target_name, signature,
             ", ".join(sorted(world_motion_bones)) or "none",
             frames_observed, report.compared_bone_count,
             _display_ratio(report.median_ratio)))

    if report.unit_mismatch:
        print("[MOVIN Live] Skeleton diagnostics suppressed: streamed lengths are %sx the rest "
              "pose on average, which is a unit mismatch rather than a calibration offset. "
              "Streamed values are metres - check the scale of armature object '%s' and "
              "Scene > Units > Unit Scale."
              % (_display_ratio(report.median_ratio), target_name))
    elif report.has_deviation():
        print("[MOVIN Live] Skeleton Calibration Offset\n"
              + format_report(subject, target_name, report))
    else:
        print("[MOVIN Live] Streamed bone lengths match armature '%s' (%d bones compared)."
              % (target_name, report.compared_bone_count))

    # Reaching here means the banner changed in some way - raised, replaced, or
    # taken down - in every branch above.
    _tag_skeleton_notice_redraw()


# -----------------------
# Application loop (main thread)
# -----------------------

def _apply_latest_stream_data(scene_name):
    global _runtime
    scene = bpy.data.scenes.get(scene_name)
    if scene is None or not hasattr(scene, "movin_props"):
        return None

    props = scene.movin_props
    arm_obj = bpy.data.objects.get(props.armature_name)
    with _runtime.lock:
        frame = _runtime.ready_frames.pop() if _runtime.ready_frames else None
        if frame is not None:
            _runtime.ready_frames.clear()
            _runtime.last_applied = frame["frame_idx"]

        pointcloud = _runtime.ready_pointclouds.pop() if _runtime.ready_pointclouds else None
        if pointcloud is not None:
            _runtime.ready_pointclouds.clear()

    did_apply = False

    if frame is not None and arm_obj is not None and arm_obj.type == 'ARMATURE':
        # Ensure POSE position so viewport shows pose changes
        if arm_obj.data.pose_position != 'POSE':
            arm_obj.data.pose_position = 'POSE'

        hips_bone_name = props.hips_bone_name.strip()
        pose_bones = arm_obj.pose.bones

        # Check for constraints that might block location changes (warn once)
        if not _runtime.warned_constraints:
            for pb in pose_bones:
                if pb.constraints:
                    print(f"[MOVIN Live] WARNING: Bone '{pb.name}' has constraints that may override location/rotation.")
                    print("  Consider disabling or removing constraints for live motion capture.")
            _runtime.warned_constraints = True

        # Rest data for the bone offsets, rebuilt only when the armature changes.
        if _runtime.bone_rest_frames_armature != arm_obj.name:
            _runtime.bone_rest_frames = collect_bone_rest_frames(arm_obj)
            _runtime.bone_rest_frames_armature = arm_obj.name
            _runtime.warned_connected_bones = False
        bone_rest_frames = _runtime.bone_rest_frames

        # Streamed metres to armature units, derived from the rig rather than
        # dialled in: see armature_units_per_metre().
        units_per_metre = armature_units_per_metre(
            arm_obj.matrix_world.to_scale(), scene.unit_settings.scale_length)

        # Blender locks the location channel of a bone connected to its parent, so
        # a calibrated offset written to one is silently dropped. Only worth saying
        # when there is actually an offset to drop: a Character stream is already
        # retargeted onto this same .fbx, so its offsets come out zero and a locked
        # channel costs nothing. Collected during the loop and reported once, so
        # the note reflects the stream rather than the rig.
        blocked_by_connect = None if _runtime.warned_connected_bones else []

        # An offset under half a millimetre is not what anyone is looking at.
        connect_report_threshold = 0.0005 * units_per_metre

        # coordinate conversions
        vec_conv = unity_to_blender_vec
        quat_conv = unity_to_blender_quat

        # index incoming by bone name
        by_name = {b["bone_name"]: b for b in frame["bones"]}

        # apply pose
        for name, bdat in by_name.items():
            pb = pose_bones.get(name)
            if pb is None:
                continue

            p = vec_conv(bdat["p"])
            rq = quat_conv(bdat["rq"])
            q = quat_conv(bdat["q"])
            rq_inv = quat_conj(rq)
            q = quat_mul(rq_inv, q)
            s = bdat["s"] # local scale wo conversion

            rest_frame = bone_rest_frames.get(name)
            streamed_units = (p[0] * units_per_metre,
                              p[1] * units_per_metre,
                              p[2] * units_per_metre)

            if name == hips_bone_name:
                # The hips translation is a world position rather than a bone
                # length, so its reference is the performer's own standing
                # height (hips_y_offset) instead of a parent-relative offset.
                # Subtracting it leaves the vertical motion, which is then added
                # to whatever height this armature's rest pose puts its hips at -
                # so the character keeps its own proportions.
                #
                # The scale is derived, not dialled in. The old fixed x100 was
                # right only for a centimetre-authored rig and overshot a
                # metre-authored one by a hundred times.
                if rest_frame is not None:
                    pb.location = rest_relative_location(
                        streamed_units,
                        (0.0, -props.hips_y_offset * units_per_metre, 0.0),
                        rest_frame["axes"])
            elif rest_frame is not None and rest_frame["offset"] is not None:
                # The calibrated offset. Both sides are in the parent's rest
                # frame: the streamed value is Unity's localPosition, and
                # collect_bone_rest_frames() puts the armature's own offset in the
                # same frame. Measuring against the rest pose rather than against
                # the first frame received is what makes this deterministic.
                #
                # A Character stream is already retargeted onto this same .fbx, so
                # its offsets equal the rest offsets and this comes out zero.
                location = rest_relative_location(
                    streamed_units, rest_frame["offset"], rest_frame["axes"])
                pb.location = location

                if blocked_by_connect is not None and rest_frame["connected"]:
                    if max(abs(v) for v in location) > connect_report_threshold:
                        blocked_by_connect.append(name)
            else:
                # Streamed but absent from the rig, or parentless and not the
                # hips. Zeroed rather than left alone, so a pose does not keep
                # offsets written by an earlier session.
                pb.location = (0.0, 0.0, 0.0)

            pb.rotation_mode = 'QUATERNION'
            pb.rotation_quaternion = (q[0], q[1], q[2], q[3])
            pb.scale = (s[0], s[1], s[2])

            # thumb offset (skinning)
            # if name == "LeftHandThumb1":
            #     pb.rotation_quaternion = quat_mul(quat_from_euler((0, 70, 0)), pb.rotation_quaternion)
            # elif name == "RightHandThumb1":
            #     pb.rotation_quaternion = quat_mul(quat_from_euler((0, -70, 0)), pb.rotation_quaternion)

        if blocked_by_connect is not None:
            _runtime.warned_connected_bones = True
            if blocked_by_connect:
                print("[MOVIN Live] NOTE: %d bone(s) carry a calibrated offset that cannot be "
                      "applied, because 'Connected' is set and Blender locks the location "
                      "channel of a connected bone: %s"
                      % (len(blocked_by_connect), ", ".join(sorted(blocked_by_connect))))
                print("  Clear 'Connected' on those bones in Edit Mode to apply the offsets.")

        did_apply = True

    if pointcloud is not None and props.pointcloud_enabled:
        sampled_points = _downsample_points(pointcloud["points"], 15000)
        converted_points = [unity_to_blender_pointcloud_vec(point) for point in sampled_points]
        _update_pointcloud_object(
            scene,
            props.pointcloud_object_name.strip() or "MOVIN_PointCloud",
            converted_points,
            0.02,
            (0.10, 0.85, 1.00, 1.00),
        )
        with _runtime.lock:
            _runtime.last_visualized_point_count = len(converted_points)
        did_apply = True

    # Guidance only - never let it take the stream down with it.
    try:
        _scan_skeleton_calibration(scene, props)
    except Exception:
        print("[MOVIN Live] Skeleton diagnostics failed:")
        traceback.print_exc()

    if not did_apply:
        return 0.03

    return 0.03

def _timer_tick(scene_name):
    try:
        return _apply_latest_stream_data(scene_name)
    except Exception:
        print("[MOVIN Live] Timer callback failed:")
        traceback.print_exc()
        return 0.5

# -----------------------
# Operators and Panel
# -----------------------

class MOVIN_OT_SelectActiveArmature(Operator):
    bl_idname = "movin.select_active_armature"
    bl_label = "Use Active Armature"
    bl_description = "Use the currently selected armature object"
    def execute(self, context):
        obj = context.active_object
        props = context.scene.movin_props
        if obj and obj.type == 'ARMATURE':
            props.armature_name = obj.name
            self.report({'INFO'}, f"Armature set to '{obj.name}'")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "Active object is not an Armature")
            return {'CANCELLED'}

class MOVIN_OT_DismissSkeletonNotice(Operator):
    bl_idname = "movin.dismiss_skeleton_notice"
    bl_label = "Dismiss"
    bl_description = "Hide this notice until the streamed skeleton changes"
    def execute(self, context):
        global _runtime
        # Only the banner is cleared. The signature stays recorded, so the same
        # figures will not come back - a recalibration will.
        with _runtime.lock:
            _runtime.skeleton_banner = None
        return {'FINISHED'}

class MOVIN_OT_Start(Operator):
    bl_idname = "movin.start_stream"
    bl_label = "Start"
    bl_description = "Start listening for /MOVIN/Frame and applying to the armature"
    _timer = None
    def execute(self, context):
        global _runtime
        props = context.scene.movin_props
        if props.is_running:
            self.report({'INFO'}, "Already running")
            return {'CANCELLED'}
        arm_obj = bpy.data.objects.get(props.armature_name) if props.armature_name else None
        has_valid_armature = arm_obj is not None and arm_obj.type == 'ARMATURE'
        if not has_valid_armature and not props.pointcloud_enabled:
            self.report({'WARNING'}, "Please select a valid Armature or enable point cloud visualization")
            return {'CANCELLED'}

        # Make sure viewport shows pose results
        if arm_obj and hasattr(arm_obj.data, "pose_position"):
            arm_obj.data.pose_position = 'POSE'

        _runtime.reset()
        _runtime.running = True
        t = threading.Thread(target=_udp_server_loop, args=(props.port,), daemon=True)
        _runtime.thread = t
        t.start()
        scene_name = context.scene.name
        self._timer = bpy.app.timers.register(lambda: _timer_tick(scene_name), first_interval=0.01, persistent=True)
        props.is_running = True
        print(f"[MOVIN Live] Listening on UDP {props.port}")
        return {'FINISHED'}

class MOVIN_OT_Stop(Operator):
    bl_idname = "movin.stop_stream"
    bl_label = "Stop"
    bl_description = "Stop listening and applying"
    def execute(self, context):
        global _runtime
        props = context.scene.movin_props
        if not props.is_running:
            self.report({'INFO'}, "Not running")
            return {'CANCELLED'}
        props.is_running = False
        _runtime.running = False
        if _runtime.sock:
            try:
                _runtime.sock.close()
            except Exception:
                pass
        time.sleep(0.05)
        _runtime.thread = None
        _runtime.sock = None
        _validation_logger.close()
        _runtime.reset()
        print("[MOVIN Live] Stopped")
        return {'FINISHED'}

class MOVIN_OT_DumpStatus(Operator):
    bl_idname = "movin.dump_status"
    bl_label = "Print Status"
    bl_description = "Print receiver status to the console"
    def execute(self, context):
        global _runtime
        with _runtime.lock:
            print("[MOVIN Live] --- Status ---")
            print(" running:", context.scene.movin_props.is_running)
            print(" last actor:", _runtime.last_actor)
            print(" last ts:", _runtime.last_ts)
            print(" last applied frame:", _runtime.last_applied)
            print(" last point cloud frame:", _runtime.last_pointcloud_frame)
            print(" last point count:", _runtime.last_pointcloud_count)
            print(" visualized point count:", _runtime.last_visualized_point_count)
            print(" received packets:", _runtime.received_packets)
            print(" frame packets:", _runtime.frame_packets)
            print(" completed frames:", _runtime.completed_frames)
            print(" last packet:", _runtime.last_packet_address, _runtime.last_packet_time, _runtime.last_packet_bytes, _runtime.last_sender)
            print(" last parse error:", _runtime.last_parse_error)
            print(" socket error:", _runtime.socket_error)
            print(" socket poll (Hz):", f"{_runtime.socket_poll_rate_hz:.1f}")
            print(" udp datagrams (Hz):", f"{_runtime.recv_rate_hz:.1f}")
            print(" completed frames (Hz):", f"{_runtime.completed_frame_rate_hz:.1f}")
            print(" queued complete frames:", len(_runtime.ready_frames))
            print(" queued point clouds:", len(_runtime.ready_pointclouds))
            print(" partials:", len(_runtime.frame_buffers))
            print(" point cloud partials:", len(_runtime.pointcloud_buffers))
            print(" skeleton subject:", _runtime.skeleton_subject or "-",
                  "(actor stream:", is_actor_subject(_runtime.skeleton_subject), ")")
            print(" skeleton frames observed:", _runtime.skeleton_frames_observed)
            print(" skeleton notified signatures:", _runtime.skeleton_notified_signatures or "{}")
            print(" skeleton notice raised:", _runtime.skeleton_banner is not None)
            world_motion = find_world_motion_bones(
                _runtime.skeleton_bone_names,
                _runtime.skeleton_length_change_counts,
                _runtime.skeleton_frames_observed)
            print(" skeleton world-motion bones:", ", ".join(sorted(world_motion)) or "none")
        return {'FINISHED'}

class MOVIN_PT_Panel(Panel):
    bl_idname = "MOVIN_PT_panel"
    bl_label = "MOVIN Live"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MOVIN Live"
    def draw(self, context):
        layout = self.layout
        layout.scale_x = 1.5
        layout.scale_y = 1
        props = context.scene.movin_props
        global _runtime

        # Drawn at the top of the panel and left up until dismissed. Blender has
        # no persistent toast - self.report() only reaches the status bar from an
        # operator and fades in seconds - and the offset is a standing property of
        # the stream rather than an event, so a message that disappeared would be
        # missed by anyone who looked at the viewport first. Deliberately INFO
        # and not a warning colour: this is intended behaviour, and dressing it
        # as a warning would create the very impression it exists to remove.
        with _runtime.lock:
            banner = _runtime.skeleton_banner
        if banner is not None:
            box = layout.box()
            box.label(text="Skeleton Calibration Offset", icon='INFO')
            col = box.column(align=True)
            for line in banner["lines"]:
                col.label(text=line)
            box.operator("movin.dismiss_skeleton_notice", text="Dismiss", icon='CHECKMARK')
            layout.separator(factor=0.5)

        col = layout.column(align=True)
        col.prop_search(props, "armature_name", bpy.data, "objects", text="Armature")
        col.operator("movin.select_active_armature", text="Use Active Armature", icon="ARMATURE_DATA")

        layout.separator(factor=0.5)
        col = layout.column(align=True)
        col.prop(props, "port")

        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="Global Transform Handling", icon="OUTLINER_OB_ARMATURE")
        row = box.row(align=True)
        row.prop(props, "hips_bone_name")
        row = box.row(align=True)
        row.prop(props, "hips_y_offset")

        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="Point Cloud", icon="MESH_DATA")
        row = box.row(align=True)
        row.prop(props, "pointcloud_enabled")
        row = box.row(align=True)
        row.prop(props, "pointcloud_object_name")

        layout.separator(factor=0.5)
        row = layout.row(align=True)
        row.enabled = not props.is_running
        row.operator("movin.start_stream", text="Start", icon="PLAY")
        row = layout.row(align=True)
        row.enabled = props.is_running
        row.operator("movin.stop_stream", text="Stop", icon="PAUSE")
        layout.operator("movin.dump_status", text="Print Status", icon="INFO")

        layout.separator(factor=0.8)
        box = layout.box()
        box.label(text="Live Status", icon="RESTRICT_VIEW_OFF")
        with _runtime.lock:
            if _runtime.socket_error:
                box.label(text=f"Socket Error: {_runtime.socket_error}", icon="ERROR")
            box.label(text=f"Actor: {_runtime.last_actor or '-'}")
            box.label(text=f"Timestamp: {_runtime.last_ts or '-'}")
            box.label(text=f"Last Frame: {_runtime.last_applied if _runtime.last_applied is not None else '-'}")
            box.label(text=f"PointCloud Frame: {_runtime.last_pointcloud_frame if _runtime.last_pointcloud_frame is not None else '-'}")
            box.label(text=f"Packets: {_runtime.received_packets} / Frame Chunks: {_runtime.frame_packets}")
            box.label(text=f"Completed Frames: {_runtime.completed_frames}")
            box.label(text=f"Last OSC: {_runtime.last_packet_address or '-'}")
            box.label(text=f"Last Sender: {_runtime.last_sender or '-'}")
            if _runtime.last_parse_error:
                box.label(text=f"Parse Error: {_runtime.last_parse_error}", icon="ERROR")
            box.label(text=f"Point Count: {_runtime.last_pointcloud_count}")
            box.label(text=f"Displayed Points: {_runtime.last_visualized_point_count}")
            box.label(text=f"Socket Poll: {_runtime.socket_poll_rate_hz:.1f} Hz / 120")
            box.label(text=f"UDP Datagrams: {_runtime.recv_rate_hz:.1f} Hz")
            box.label(text=f"Completed Frames: {_runtime.completed_frame_rate_hz:.1f} Hz / 60")
            box.label(text=f"Queued Frames: {len(_runtime.ready_frames)}")
            box.label(text=f"Queued PointClouds: {len(_runtime.ready_pointclouds)}")

# -----------------------
# Registration
# -----------------------

classes = (
    MOVIN_Props,
    MOVIN_OT_SelectActiveArmature,
    MOVIN_OT_DismissSkeletonNotice,
    MOVIN_OT_Start,
    MOVIN_OT_Stop,
    MOVIN_OT_DumpStatus,
    MOVIN_PT_Panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.movin_props = PointerProperty(type=MOVIN_Props)

def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.Scene, "movin_props"):
        del bpy.types.Scene.movin_props

if __name__ == "__main__":
    register()
