bl_info = {
    "name": "MOVIN Live Receiver",
    "author": "MOVIN",
    "version": (3, 3, 0),
    "blender": (4, 3, 2),
    "location": "View3D > N-Panel > MOVIN Live",
    "description": "Receives motion and point clouds from MOVIN Studio.",
    "category": "Animation",
}

import bpy
from bpy.props import (
    PointerProperty, StringProperty, IntProperty, BoolProperty, EnumProperty
)
from bpy.types import (
    PropertyGroup, Panel, Operator
)
import socket
import struct
import threading
import time
import traceback
from collections import deque, namedtuple
from mathutils import Quaternion, Vector
import math
import numpy as np

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
        s = self.data[start:end].decode('utf-8', errors='strict')
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
        if argspec and not argspec.strip('if'):
            # Point clouds contain only numbers; unpack the whole payload in C.
            size = 4 * len(argspec)
            if self.i + size != self.n:
                raise ValueError("Invalid OSC numeric payload length")
            args = list(struct.unpack_from('>' + argspec, self.data, self.i))
            self.i += size
        else:
            for t in argspec:
                if t == 'i':
                    args.append(self._read_int32())
                elif t == 'f':
                    args.append(self._read_float32())
                elif t == 's':
                    args.append(self._read_padded_string())
                else:
                    raise ValueError(f"Unsupported OSC arg type: {t}")
        if self.i != self.n or not address.startswith('/'):
            raise ValueError("Invalid OSC message length or address")
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

MOTION_BUFFER_MAX_AGE = 0.05

class chunk_stream:
    """Bounded assembly and ordering for one motion or point-cloud source."""
    def __init__(self, buffers, ready):
        self.buffers = buffers
        self.ready = ready
        self.sender = None
        self.last_frame = -1
        self.last_time = 0.0
        self.received_times = deque(maxlen=240)

    def expire(self, now):
        for key in [k for k, v in self.buffers.items() if now - v["time"] >= 0.5]:
            del self.buffers[key]

    def add(self, sender, index, metadata, count, chunk, items, now):
        self.expire(now)
        same = sender == self.sender
        # A restarted index is accepted after one second without a newer frame.
        ordered = index > self.last_frame or (index < self.last_frame and now - self.last_time >= 1.0)
        available = self.sender is None or same or now - self.last_time >= 2.0
        result = None
        if available and (not same or ordered):
            key = (sender, index)
            buf = self.buffers.get(key)
            if buf is None:
                if len(self.buffers) >= 8:
                    del self.buffers[next(iter(self.buffers))]
                buf = {"time": now, "metadata": metadata, "count": count, "chunks": {}, "size": 0}
                self.buffers[key] = buf
            if buf["metadata"] != metadata or buf["count"] != count:
                del self.buffers[key]
                raise ValueError("Conflicting chunk metadata")
            previous = buf["chunks"].get(chunk)
            if previous is not None and previous != items:
                del self.buffers[key]
                raise ValueError("Conflicting duplicate chunk")
            if previous is None:
                buf["size"] += len(items)
            buf["chunks"][chunk] = items
            if buf["size"] > metadata["total"]:
                del self.buffers[key]
                raise ValueError("Chunks exceed the declared frame size")
            if len(buf["chunks"]) == count:
                del self.buffers[key]
                joined = [item for i in range(count) for item in buf["chunks"][i]]
                if len(joined) != metadata["total"]:
                    raise ValueError("Frame item count does not match its header")
                if "actor" in metadata:
                    indices = {b["bone_index"] for b in joined}
                    names = {b["bone_name"] for b in joined}
                    if len(indices) != len(joined) or len(names) != len(joined):
                        raise ValueError("Duplicate bone index or name")
                    if any(b["parent_index"] != -1 and b["parent_index"] not in indices for b in joined):
                        raise ValueError("Missing parent bone")
                    joined.sort(key=lambda b: b["bone_index"])
                if self.sender is not None and (not same or index <= self.last_frame):
                    self.received_times.clear()
                    self.buffers.clear()
                    self.ready.clear()
                else:
                    for old in [k for k in self.buffers if k[0] != sender or k[1] <= index]:
                        del self.buffers[old]
                if "actor" not in metadata or (self.ready and self.ready[-1]["actor"] != metadata["actor"]):
                    self.ready.clear()
                self.sender, self.last_frame, self.last_time = sender, index, now
                self.received_times.append(now)
                result = dict(metadata, frame_idx=index, received_at=now)
                result["bones" if "actor" in metadata else "points"] = joined
                self.ready.append(result)
        return result


def frame_rate(times, now):
    return float(sum(t > now - 1.0 for t in times))


class MOVINRuntime:
    def __init__(self):
        self.thread = None
        self.sock = None
        self.running = False
        self.scene_name = ""
        self.lock = threading.Lock()
        self.frame_buffers = {}
        self.pointcloud_buffers = {}
        # A third complete frame replaces the oldest, never growing the delay.
        self.ready_frames = deque(maxlen=2)
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
        self.warned_constraints = False  # NEW: one-time console warning
        self._reset_streams()
        self._reset_skeleton_diagnostics()

    def _reset_streams(self):
        self.motion = chunk_stream(self.frame_buffers, self.ready_frames)
        self.cloud = chunk_stream(self.pointcloud_buffers, self.ready_pointclouds)
        self.applied_times = deque(maxlen=240)
        self.status_request = None
        self.status_sent = 0.0
        self.matched = 0
        self.missing = 0

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
        every frame - the timer may discard frames when overloaded and would
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
            self.warned_constraints = False
            self._reset_streams()
            self._reset_skeleton_diagnostics()

_runtime = MOVINRuntime()

# -----------------------
# Properties
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
        description="Bone whose world position follows the streamed hips position",
        default="Hips"
    )
    is_running: BoolProperty(
        name="Running",
        get=lambda self: _runtime.running and _runtime.scene_name == self.id_data.name,
        options={'SKIP_SAVE'}
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

def _receive_packet(data, sender, now):
    address, args = _OscReader(data).read_message()
    _runtime.last_packet_address = address

    def integer(value, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError("Invalid integer in streaming packet")
        return value

    def text(value, maximum):
        if not isinstance(value, str) or not 0 < len(value) <= maximum:
            raise ValueError("Invalid string in streaming packet")
        return value

    if address == "/MOVIN/Blender/Status/Request":
        if len(args) != 2:
            raise ValueError("Invalid status request")
        token = text(args[0], 32)
        if len(token) != 32 or any(c not in "0123456789abcdefABCDEF" for c in token):
            raise ValueError("Invalid status token")
        port = integer(args[1], 1, 65535)
        if now - _runtime.status_sent >= 0.25:
            _runtime.status_request = (sender, port, token)
    elif address == "/MOVIN/Frame":
        if len(args) < 7:
            raise ValueError("Truncated motion header")
        stamp, actor = text(args[0], 64), text(args[1], 256)
        index = integer(args[2], 0, 2147483647)
        count = integer(args[3], 1, 4096)
        chunk = integer(args[4], 0, count - 1)
        total = integer(args[5], 1, 4096)
        size = integer(args[6], 1, total)
        if count > total or len(args) != 7 + size * 17:
            raise ValueError("Invalid motion chunk size")
        bones = []
        for k in range(7, len(args), 17):
            bone_index = integer(args[k], 0, 4095)
            parent = integer(args[k + 1], -1, bone_index - 1)
            name = text(args[k + 2], 256)
            values = args[k + 3:k + 17]
            if any(type(v) is not float or not math.isfinite(v) for v in values):
                raise ValueError("Non-finite or invalid bone transform")
            px, py, pz, rx, ry, rz, rw, qx, qy, qz, qw, sx, sy, sz = values
            rn = math.sqrt(rx*rx + ry*ry + rz*rz + rw*rw)
            qn = math.sqrt(qx*qx + qy*qy + qz*qz + qw*qw)
            if rn < 1e-6 or qn < 1e-6:
                raise ValueError("Zero bone rotation")
            bones.append({"bone_index": bone_index, "parent_index": parent, "bone_name": name,
                          "p": (px, py, pz), "rq": (rw/rn, rx/rn, ry/rn, rz/rn),
                          "q": (qw/qn, qx/qn, qy/qn, qz/qn), "s": (sx, sy, sz)})
        _runtime.frame_packets += 1
        frame = _runtime.motion.add(sender, index, {"timestamp": stamp, "actor": actor, "total": total}, count, chunk, bones, now)
        if frame is not None:
            _runtime.last_parse_error = ""
            _runtime.last_actor, _runtime.last_ts = actor, stamp
            _runtime.completed_frames += 1
            _runtime.note_skeleton_locked(actor, frame["bones"])
    elif address == "/MOVIN/PointCloud":
        if len(args) < 5:
            raise ValueError("Truncated point cloud header")
        index = integer(args[0], 0, 2147483647)
        total = integer(args[1], 0, 200000)
        count = integer(args[3], 1, max(1, total))
        chunk = integer(args[2], 0, count - 1)
        size = integer(args[4], 0, total)
        if len(args) != 5 + size * 3 or (size == 0 and (count != 1 or total != 0)):
            raise ValueError("Invalid point cloud chunk size")
        values = args[5:]
        if any(type(v) is not float or not math.isfinite(v) for v in values):
            raise ValueError("Non-finite or invalid point cloud position")
        points = list(zip(values[::3], values[1::3], values[2::3]))
        cloud = _runtime.cloud.add(sender, index, {"total": total}, count, chunk, points, now)
        if cloud is not None:
            _runtime.last_parse_error = ""
            _runtime.last_pointcloud_frame = index
            _runtime.last_pointcloud_count = total


def _udp_server_loop(sock):
    try:
        while _runtime.running:
            try:
                data, sender = sock.recvfrom(65535)
                now = time.monotonic()
                with _runtime.lock:
                    _runtime.motion.expire(now)
                    _runtime.cloud.expire(now)
                    _runtime.received_packets += 1
                    _runtime.last_sender = f"{sender[0]}:{sender[1]}"
                    _runtime.last_packet_bytes = len(data)
                    _runtime.last_packet_time = time.strftime("%H:%M:%S")
                    _receive_packet(data, sender, now)
            except socket.timeout:
                with _runtime.lock:
                    now = time.monotonic()
                    _runtime.motion.expire(now)
                    _runtime.cloud.expire(now)
            except (ValueError, IndexError, OverflowError) as e:
                with _runtime.lock:
                    if _runtime.last_parse_error != str(e):
                        print("[MOVIN Live] Packet rejected:", e)
                    _runtime.last_parse_error = str(e)
    except OSError as e:
        if _runtime.running:
            _runtime.socket_error = str(e)
            print("[MOVIN Live] Socket failed:", e)
    except Exception as e:
        _runtime.socket_error = str(e)
        traceback.print_exc()
    finally:
        _runtime.running = False
        sock.close()


def _stop_receiver():
    _runtime.running = False
    if bpy.app.timers.is_registered(_timer_tick):
        bpy.app.timers.unregister(_timer_tick)
    if _runtime.sock is not None:
        _runtime.sock.close()
    if _runtime.thread is not None:
        _runtime.thread.join(timeout=2.0)
        if _runtime.thread.is_alive():
            raise RuntimeError("MOVIN receiver did not stop")
    _runtime.thread = None
    _runtime.sock = None
    _runtime.scene_name = ""
    _runtime.reset()


def _before_file_load(unused):
    _stop_receiver()


# -----------------------
# Coordinate & Math
# -----------------------

def unity_to_blender_vec(v):
    return (-v[0], v[1], v[2])

def unity_to_blender_world_vec(v):
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
    node_group = _ensure_pointcloud_gn_tree()
    if modifier.node_group != node_group:
        modifier.node_group = node_group
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
            for name, value in (("Base Color", rgba), ("Emission Color", rgba),
                                ("Emission Strength", 1.0), ("Roughness", 0.35)):
                current = principled.inputs[name].default_value
                if isinstance(value, tuple):
                    current = tuple(current)
                    value = tuple(float(np.float32(v)) for v in value)
                else:
                    value = float(np.float32(value))
                if current != value:
                    principled.inputs[name].default_value = value
    rgba = tuple(float(np.float32(v)) for v in rgba)
    if tuple(mat.diffuse_color) != rgba:
        mat.diffuse_color = rgba
    return mat

def _ensure_pointcloud_object(scene, object_name):
    obj = bpy.data.objects.get(object_name)
    if obj is not None and obj.type == 'MESH' and obj.modifiers.get("MOVIN_PointCloud") is not None:
        return obj

    if obj is not None:
        raise ValueError(f"'{object_name}' already exists. Choose a different point cloud object name.")

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
    mesh = obj.data
    if len(mesh.vertices) != len(points):
        mesh.clear_geometry()
        mesh.vertices.add(len(points))
    mesh.vertices.foreach_set("co", np.asarray(points, dtype=np.float32).ravel())
    mesh.update()

    modifier = _ensure_pointcloud_modifier(obj)
    radius = float(np.float32(radius))
    if bpy.app.version >= (5, 2, 0):
        socket = modifier.properties.inputs.Socket_2
        if socket.value != radius:
            socket.value = radius
    elif modifier.get("Socket_2") != radius:
        modifier["Socket_2"] = radius

    material = _ensure_pointcloud_material(object_name + "_MAT", color_rgba)
    if len(mesh.materials) == 0:
        mesh.materials.append(material)
    elif mesh.materials[0] != material:
        mesh.materials[0] = material
    color_rgba = tuple(float(np.float32(v)) for v in color_rgba)
    if tuple(obj.color) != color_rgba:
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
        now = time.monotonic()
        while _runtime.ready_frames and now - _runtime.ready_frames[0]["received_at"] >= MOTION_BUFFER_MAX_AGE:
            _runtime.ready_frames.popleft()
        frame = _runtime.ready_frames.popleft() if _runtime.ready_frames else None

        pointcloud = _runtime.ready_pointclouds.pop() if _runtime.ready_pointclouds else None
        if pointcloud is not None:
            _runtime.ready_pointclouds.clear()


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

        by_index = {b["bone_index"]: b for b in frame["bones"]}
        hips = pose_bones.get(hips_bone_name) if hips_bone_name in by_name else None
        if hips is not None and hips.bone.use_connect:
            raise ValueError(
                f"Hips bone '{hips.name}' is Connected to its parent. "
                "Clear Connected in Edit Mode to apply the streamed world position.")
        consumed = set()
        matched = 0
        for name, bdat in by_name.items():
            pb = pose_bones.get(name)
            if pb is None:
                continue

            matched += 1
            p, rq, q, s = bdat["p"], bdat["rq"], bdat["q"], bdat["s"]
            parent = bdat["parent_index"]
            while parent >= 0 and by_index[parent]["bone_name"] not in pose_bones:
                ancestor = by_index[parent]
                rotated = rotate_vec(tuple(a * b for a, b in zip(p, ancestor["s"])), ancestor["q"])
                p = tuple(a + b for a, b in zip(ancestor["p"], rotated))
                rq, q = quat_mul(ancestor["rq"], rq), quat_mul(ancestor["q"], q)
                s = tuple(a * b for a, b in zip(ancestor["s"], s))
                consumed.add(ancestor["bone_name"])
                parent = ancestor["parent_index"]
            p, rq, q = vec_conv(p), quat_conv(rq), quat_conv(q)
            rq_inv = quat_conj(rq)
            q = quat_mul(rq_inv, q)

            rest_frame = bone_rest_frames.get(name)
            streamed_units = (p[0] * units_per_metre,
                              p[1] * units_per_metre,
                              p[2] * units_per_metre)

            if name != hips_bone_name:
                if rest_frame is not None and rest_frame["offset"] is not None:
                    # Subtract the rig's rest offset to preserve streamed bone lengths.
                    location = rest_relative_location(
                        streamed_units, rest_frame["offset"], rest_frame["axes"])
                    pb.location = location

                    if blocked_by_connect is not None and rest_frame["connected"]:
                        if max(abs(v) for v in location) > connect_report_threshold:
                            blocked_by_connect.append(name)
                else:
                    pb.location = rest_relative_location(
                        streamed_units, tuple(pb.bone.head_local), rest_frame["axes"])

            pb.rotation_mode = 'QUATERNION'
            pb.rotation_quaternion = (q[0], q[1], q[2], q[3])
            pb.scale = (s[0], s[1], s[2])

            # thumb offset (skinning)
            # if name == "LeftHandThumb1":
            #     pb.rotation_quaternion = quat_mul(quat_from_euler((0, 70, 0)), pb.rotation_quaternion)
            # elif name == "RightHandThumb1":
            #     pb.rotation_quaternion = quat_mul(quat_from_euler((0, -70, 0)), pb.rotation_quaternion)

        if hips is not None:
            # Reconstruct the source world point before any missing-parent folding.
            bdat = by_name[hips_bone_name]
            p, parent = bdat["p"], bdat["parent_index"]
            while parent >= 0:
                ancestor = by_index[parent]
                rotated = rotate_vec(tuple(a * b for a, b in zip(p, ancestor["s"])), ancestor["q"])
                p = tuple(a + b for a, b in zip(ancestor["p"], rotated))
                parent = ancestor["parent_index"]
            world = Vector(unity_to_blender_world_vec(p)) / scene.unit_settings.scale_length
            target = arm_obj.matrix_world.inverted() @ world

            # Evaluate this frame's parent channels; PoseBone.matrix may still be stale.
            parent = None
            for pb in reversed([hips] + list(hips.parent_recursive)):
                args = {} if parent is None else {
                    "parent_matrix": pose,
                    "parent_matrix_local": parent.bone.matrix_local,
                }
                pose = pb.bone.convert_local_to_pose(pb.matrix_basis, pb.bone.matrix_local, **args)
                if pb == hips:
                    pose.translation = target
                    basis = pb.bone.convert_local_to_pose(
                        pose, pb.bone.matrix_local, invert=True, **args)
                    pb.location = basis.translation
                parent = pb

        missing = len(by_name) - matched - len(consumed)
        if blocked_by_connect is not None:
            _runtime.warned_connected_bones = True
            if blocked_by_connect:
                print("[MOVIN Live] NOTE: %d bone(s) carry a calibrated offset that cannot be "
                      "applied, because 'Connected' is set and Blender locks the location "
                      "channel of a connected bone: %s"
                      % (len(blocked_by_connect), ", ".join(sorted(blocked_by_connect))))
                print("  Clear 'Connected' on those bones in Edit Mode to apply the offsets.")

        with _runtime.lock:
            _runtime.matched = matched
            _runtime.missing = missing
            if matched > 0:
                _runtime.last_applied = frame["frame_idx"]
                _runtime.applied_times.append(time.monotonic())

    if pointcloud is not None and props.pointcloud_enabled:
        sampled_points = _downsample_points(pointcloud["points"], 15000)
        units = armature_units_per_metre((1.0, 1.0, 1.0), scene.unit_settings.scale_length)
        converted_points = np.asarray(sampled_points, dtype=np.float32).reshape(-1, 3)[:, (0, 2, 1)]
        converted_points *= (-units, -units, units)
        _update_pointcloud_object(
            scene,
            props.pointcloud_object_name.strip() or "MOVIN_PointCloud",
            converted_points,
            0.02 * units,
            (0.10, 0.85, 1.00, 1.00),
        )
        with _runtime.lock:
            _runtime.last_visualized_point_count = len(converted_points)

    # Guidance only - never let it take the stream down with it.
    try:
        _scan_skeleton_calibration(scene, props)
    except Exception:
        print("[MOVIN Live] Skeleton diagnostics failed:")
        traceback.print_exc()

    return 1.0 / 120.0

def _timer_tick():
    started = time.perf_counter()
    interval = None
    if _runtime.running:
        try:
            interval = _apply_latest_stream_data(_runtime.scene_name)
            with _runtime.lock:
                request = _runtime.status_request
                _runtime.status_request = None
            if request is not None:
                sender, port, token = request
                scene = bpy.data.scenes[_runtime.scene_name]
                arm = bpy.data.objects.get(scene.movin_props.armature_name)
                valid = arm is not None and arm.type == 'ARMATURE'
                now = time.monotonic()
                with _runtime.lock:
                    motion, cloud = _runtime.motion, _runtime.cloud
                    applied_here = valid and _runtime.bone_rest_frames_armature == arm.name
                    args = [token, 1, arm.name[:256] if valid else "", len(arm.pose.bones) if valid else 0,
                            _runtime.matched if applied_here else 0, _runtime.missing if applied_here else 0,
                            frame_rate(motion.received_times, now), frame_rate(_runtime.applied_times, now) if applied_here else 0.0,
                            frame_rate(cloud.received_times, now),
                            float(now - motion.last_time) if motion.sender else -1.0,
                            float(now - cloud.last_time) if cloud.sender else -1.0,
                            int(sender == motion.sender), int(sender == cloud.sender),
                            _runtime.last_parse_error[:256]]
                    _runtime.status_sent = now
                def osc_string(value):
                    value = value.encode("utf-8") + b"\0"
                    return value + b"\0" * (-len(value) % 4)
                tags = ",sisiiifffffiis"
                packet = osc_string("/MOVIN/Blender/Status") + osc_string(tags)
                for tag, value in zip(tags[1:], args):
                    packet += osc_string(value) if tag == "s" else struct.pack(">" + tag, value)
                _runtime.sock.sendto(packet, (sender[0], port))
            if interval is None:
                _stop_receiver()
        except Exception as e:
            traceback.print_exc()
            _stop_receiver()
            _runtime.socket_error = str(e)
    if interval is not None and _runtime.running:
        # Blender waits after the callback; include our work in the polling period.
        interval = max(0.001, interval - (time.perf_counter() - started))
    return interval if _runtime.running else None


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

        _stop_receiver()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
            sock.bind(("0.0.0.0", props.port))
            sock.settimeout(0.05)
            _runtime.sock = sock
            _runtime.scene_name = context.scene.name
            _runtime.running = True
            _runtime.thread = threading.Thread(target=_udp_server_loop, args=(sock,), daemon=True)
            _runtime.thread.start()
            bpy.app.timers.register(_timer_tick, first_interval=0.0)
        except OSError as e:
            sock.close()
            _stop_receiver()
            _runtime.socket_error = f"Cannot listen on port {props.port}: {e}"
            self.report({'ERROR'}, _runtime.socket_error)
            return {'CANCELLED'}
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
        _stop_receiver()
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
            print(" received motion FPS:", frame_rate(_runtime.motion.received_times, time.monotonic()))
            print(" received point cloud FPS:", frame_rate(_runtime.cloud.received_times, time.monotonic()))
            print(" applied motion FPS:", frame_rate(_runtime.applied_times, time.monotonic()))
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
        box.label(text="Global Hips Position", icon="OUTLINER_OB_ARMATURE")
        row = box.row(align=True)
        row.prop(props, "hips_bone_name")

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
            now = time.monotonic()
            box.label(text=f"Received FPS: {frame_rate(_runtime.motion.received_times, now):.1f}")
            box.label(text=f"Applied FPS: {frame_rate(_runtime.applied_times, now):.1f}")
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
    bpy.app.handlers.persistent(_before_file_load)
    bpy.app.handlers.load_pre.append(_before_file_load)

def unregister():
    _stop_receiver()
    bpy.app.handlers.load_pre.remove(_before_file_load)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.Scene, "movin_props"):
        del bpy.types.Scene.movin_props

if __name__ == "__main__":
    register()
