#!/usr/bin/env python3
"""
vk_capture_consumer.py - Standalone obs-vkcapture consumer.

v14 fixes the v13 window inversion:
  - healthy_streak_start is set only on the first frame of an unbroken run
  - any None frame or reject disarms the streak
  - amnesty fires only after reset_window seconds of continuous delivery

Requires: Linux, Python 3.10+, numpy, ffmpeg on PATH.
"""
import argparse
import fcntl
import json
import mmap
import os
import queue
import socket
import struct
import subprocess
import threading
import time
from collections import deque, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

SOCK_PATH = b"/com/obsproject/vkcapture"
CAPTURE_CLIENT_DATA_TYPE = 10
CAPTURE_TEXTURE_DATA_TYPE = 11
CAPTURE_CONTROL_DATA_SIZE = 32
CAPTURE_CLIENT_DATA_SIZE = 128
CAPTURE_TEXTURE_DATA_SIZE = 128

DMA_BUF_IOCTL_SYNC = 0x40086200
DMA_BUF_SYNC_READ = 0x01
DMA_BUF_SYNC_START = 0x00
DMA_BUF_SYNC_END = 0x04

DRM_FORMAT_ARGB8888 = 0x34325241
DRM_FORMAT_XRGB8888 = 0x34325258
DRM_FORMAT_ABGR8888 = 0x34324241
DRM_FORMAT_XBGR8888 = 0x34324258

FORMAT_TO_PIXFMT = {
    DRM_FORMAT_ARGB8888: "bgra",
    DRM_FORMAT_XRGB8888: "bgr0",
    DRM_FORMAT_ABGR8888: "rgba",
    DRM_FORMAT_XBGR8888: "rgb0",
}

CLIENT_DATA_FMT = "<B48s79s"
TEXTURE_DATA_FMT = "<BB" + "i" * 11 + "QIBI65s"
CONTROL_DATA_FMT = "<BBBB16s12s"
assert struct.calcsize(CLIENT_DATA_FMT) == CAPTURE_CLIENT_DATA_SIZE
assert struct.calcsize(TEXTURE_DATA_FMT) == CAPTURE_TEXTURE_DATA_SIZE
assert struct.calcsize(CONTROL_DATA_FMT) == CAPTURE_CONTROL_DATA_SIZE

MAX_REJECTS = 3

_lifetime_rejects: dict = defaultdict(int)
_lifetime_lock = threading.Lock()


@dataclass
class TextureState:
    width: int = 0
    height: int = 0
    format: int = 0
    strides: tuple = (0, 0, 0, 0)
    offsets: tuple = (0, 0, 0, 0)
    modifier: int = 0
    winid: int = 0
    flip: bool = False
    color_space: int = 0
    buf_fds: list = field(default_factory=list)
    mm: Optional[mmap.mmap] = None
    map_size: int = 0

    def teardown(self):
        if self.mm is not None:
            try:
                self.mm.close()
            except (BufferError, ValueError):
                pass
            self.mm = None
        for fd in self.buf_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.buf_fds = []
        self.map_size = 0


def parse_client_data(buf: bytes):
    msg_type, exe, _ = struct.unpack(CLIENT_DATA_FMT, buf)
    return msg_type, exe.split(b"\x00", 1)[0].decode(errors="replace")


def parse_texture_data(buf: bytes):
    (msg_type, nfd,
     w, h, fmt,
     s0, s1, s2, s3,
     o0, o1, o2, o3,
     modifier, winid, flip, color_space,
     _pad) = struct.unpack(TEXTURE_DATA_FMT, buf)
    return TextureState(
        width=w, height=h, format=fmt,
        strides=(s0, s1, s2, s3),
        offsets=(o0, o1, o2, o3),
        modifier=modifier, winid=winid,
        flip=bool(flip), color_space=color_space,
    ), nfd


def build_control(capturing=True, no_modifiers=False, linear=False,
                  map_host=False, device_uuid=b"\x00" * 16) -> bytes:
    return struct.pack(
        CONTROL_DATA_FMT,
        1 if capturing else 0,
        1 if no_modifiers else 0,
        1 if linear else 0,
        1 if map_host else 0,
        device_uuid[:16],
        b"\x00" * 12,
    )


def recv_msg_with_fds(sock, maxfds=4):
    msg, ancdata, _flags, _addr = sock.recvmsg(
        CAPTURE_TEXTURE_DATA_SIZE, socket.CMSG_SPACE(4 * maxfds),
    )
    fds = []
    for cmsg_level, cmsg_type, cmsg_data in ancdata:
        if cmsg_level == socket.SOL_SOCKET and cmsg_type == socket.SCM_RIGHTS:
            n = len(cmsg_data) // 4
            for i in range(n):
                fds.append(struct.unpack("i", cmsg_data[i * 4:(i + 1) * 4])[0])
    return msg, fds


def dma_buf_sync(fd: int, start: bool, read: bool):
    flags = (DMA_BUF_SYNC_START if start else DMA_BUF_SYNC_END)
    if read:
        flags |= DMA_BUF_SYNC_READ
    buf = bytearray(8)
    struct.pack_into("<Q", buf, 0, flags)
    fcntl.ioctl(fd, DMA_BUF_IOCTL_SYNC, buf)


def map_dmabuf(fd: int):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        size = os.lseek(fd, 0, os.SEEK_END)
        if size <= 0:
            return None, 0
        mm = mmap.mmap(fd, size, prot=mmap.PROT_READ, flags=mmap.MAP_SHARED)
        return mm, size
    except OSError:
        return None, 0


def read_frame(tex: TextureState) -> Optional[np.ndarray]:
    if tex.mm is None or not tex.buf_fds:
        return None
    if tex.width <= 0 or tex.height <= 0:
        return None
    if tex.format not in FORMAT_TO_PIXFMT:
        return None
    stride = tex.strides[0] if tex.strides[0] else tex.width * 4
    offset = tex.offsets[0] if tex.offsets[0] > 0 else 0
    needed = stride * tex.height
    if offset + needed > tex.map_size:
        return None
    fd0 = tex.buf_fds[0]
    try:
        dma_buf_sync(fd0, start=True, read=True)
        try:
            raw = np.frombuffer(tex.mm, dtype=np.uint8, count=needed, offset=offset)
            frame = raw.reshape((tex.height, stride // 4, 4))[:, :tex.width, :]
            if tex.flip:
                frame = frame[::-1, :, :]
            return np.ascontiguousarray(frame)
        finally:
            dma_buf_sync(fd0, start=False, read=True)
    except (OSError, ValueError, IndexError):
        return None


class Client:
    def __init__(self, conn: socket.socket, exe_filter: Optional[str] = None,
                 lifetime_cap: int = 10):
        self.conn = conn
        self.exe = ""
        self.exe_filter = exe_filter
        self.lifetime_cap = lifetime_cap
        self.tex = TextureState()
        self.lock = threading.Lock()
        self.running = True
        self.accepted = False
        self.rejects = 0
        self.last_flags = {"capturing": False, "no_modifiers": False,
                           "linear": False, "map_host": False}
        # 0.0 = disarmed. Nonzero = timestamp when the current unbroken
        # streak of delivered frames began.
        self.healthy_streak_start = 0.0

    def send_control(self, **kwargs) -> bool:
        flags = {
            "capturing": kwargs.get("capturing", True),
            "no_modifiers": kwargs.get("no_modifiers", False),
            "linear": kwargs.get("linear", False),
            "map_host": kwargs.get("map_host", False),
        }
        if flags == self.last_flags:
            return True
        self.last_flags = flags
        try:
            self.conn.sendall(build_control(**flags,
                                            device_uuid=kwargs.get("device_uuid", b"\x00" * 16)))
            return True
        except OSError:
            self.running = False
            return False

    def request_retry(self) -> bool:
        self.rejects += 1
        self.healthy_streak_start = 0.0  # failure breaks the streak
        with _lifetime_lock:
            _lifetime_rejects[self.exe] += 1
            lifetime = _lifetime_rejects[self.exe]
        if lifetime > self.lifetime_cap:
            print(f"[!] {self.exe} exceeded lifetime reject cap "
                  f"({lifetime}/{self.lifetime_cap}); refusing reconnects")
            self.running = False
            return False
        if self.rejects == 1:
            print(f"[*] Texture rejected ({self.rejects}/{MAX_REJECTS}); "
                  f"escalating no_modifiers=1")
            return self.send_control(capturing=True, no_modifiers=True,
                                     linear=True, map_host=True)
        if self.rejects == 2:
            print(f"[!] Texture rejected again ({self.rejects}/{MAX_REJECTS}); "
                  f"forcing stop/init cycle")
            self.send_control(capturing=False, no_modifiers=True,
                              linear=True, map_host=True)
            time.sleep(0.05)
            return self.send_control(capturing=True, no_modifiers=True,
                                     linear=True, map_host=True)
        print(f"[!] Texture rejected {self.rejects} times; dropping client")
        return False

    def teardown(self):
        if self.accepted:
            self.send_control(capturing=False, map_host=True, linear=True)
        self.running = False
        with self.lock:
            self.tex.teardown()
        try:
            self.conn.close()
        except OSError:
            pass


def reject_texture(client: Client, fds: list) -> bool:
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass
    return client.request_retry()


def handle_connection(client: Client, analyzer_q: queue.Queue,
                      poll_hz: float, connect_timeout: float,
                      reset_window: float):
    sock = client.conn
    sock.settimeout(1.0)

    deadline = time.monotonic() + connect_timeout

    while client.running and client.tex.mm is None:
        if time.monotonic() > deadline:
            print(f"[!] Handshake deadline exceeded ({connect_timeout}s); "
                  f"dropping {client.exe or '<unknown>'}")
            client.teardown()
            return
        try:
            msg, fds = recv_msg_with_fds(sock)
        except socket.timeout:
            continue
        except OSError:
            client.teardown()
            return
        if not msg:
            client.teardown()
            return

        msg_type = msg[0]
        if msg_type == CAPTURE_CLIENT_DATA_TYPE and len(msg) == CAPTURE_CLIENT_DATA_SIZE:
            _t, client.exe = parse_client_data(msg)
            print(f"[*] Client exe: {client.exe}")
            if client.exe_filter and client.exe_filter not in client.exe:
                print(f"[*] Filter reject: {client.exe}")
                client.teardown()
                return
            client.send_control(capturing=True, map_host=True, linear=True)
        elif msg_type == CAPTURE_TEXTURE_DATA_TYPE and len(msg) == CAPTURE_TEXTURE_DATA_SIZE:
            tex, nfd = parse_texture_data(msg)
            if len(fds) != nfd or nfd <= 0:
                print(f"[!] FD count mismatch: header={nfd} got={len(fds)}")
                for fd in fds:
                    os.close(fd)
                client.teardown()
                return
            if nfd != 1:
                print(f"[!] Multi-plane export (nfd={nfd})")
                if not reject_texture(client, fds):
                    client.teardown()
                    return
                continue
            mm, size = map_dmabuf(fds[0])
            if mm is None:
                print(f"[!] mmap failed on fd set")
                if not reject_texture(client, fds):
                    client.teardown()
                    return
                continue
            tex.buf_fds = fds
            tex.mm = mm
            tex.map_size = size
            with client.lock:
                client.tex.teardown()
                client.tex = tex
            client.accepted = True
            client.rejects = 0
            with _lifetime_lock:
                _lifetime_rejects[client.exe] = max(0, _lifetime_rejects[client.exe] - 1)
            print(f"[+] Texture: {tex.width}x{tex.height} fmt=0x{tex.format:x} "
                  f"stride={tex.strides[0]} nfd={nfd} "
                  f"(lifetime rejects: {_lifetime_rejects[client.exe]})")

    if not client.accepted:
        client.teardown()
        return

    sock.setblocking(False)
    next_tick = time.monotonic()
    frame_dt = 1.0 / poll_hz
    while client.running:
        while True:
            try:
                msg, fds = recv_msg_with_fds(sock)
            except BlockingIOError:
                break
            except OSError:
                client.teardown()
                return
            if not msg:
                client.teardown()
                return
            if msg[0] == CAPTURE_TEXTURE_DATA_TYPE and len(msg) == CAPTURE_TEXTURE_DATA_SIZE:
                tex, nfd = parse_texture_data(msg)
                if len(fds) != nfd or nfd <= 0:
                    print(f"[!] reinit FD mismatch: header={nfd} got={len(fds)}")
                    for fd in fds:
                        try: os.close(fd)
                        except OSError: pass
                    client.teardown()
                    return
                if nfd != 1:
                    print(f"[!] Reinit multi-plane (nfd={nfd})")
                    if not reject_texture(client, fds):
                        client.teardown()
                        return
                    continue
                mm, size = map_dmabuf(fds[0])
                if mm is None:
                    print(f"[!] Reinit mmap failed")
                    if not reject_texture(client, fds):
                        client.teardown()
                        return
                    continue
                tex.buf_fds = fds
                tex.mm = mm
                tex.map_size = size
                with client.lock:
                    client.tex.teardown()
                    client.tex = tex
                client.rejects = 0
                with _lifetime_lock:
                    _lifetime_rejects[client.exe] = max(0, _lifetime_rejects[client.exe] - 1)
                print(f"[+] Reinit: {tex.width}x{tex.height} fmt=0x{tex.format:x}")

        now = time.monotonic()
        if now >= next_tick:
            next_tick = now + frame_dt
            with client.lock:
                tex = client.tex
                frame = read_frame(tex)
                meta = (tex.width, tex.height, tex.format)
            if frame is not None:
                # Frame delivered: start the streak if idle; keep it running
                # if already active.
                if client.healthy_streak_start == 0.0:
                    client.healthy_streak_start = now
                try:
                    analyzer_q.put_nowait((time.time(), frame, meta, client.exe))
                except queue.Full:
                    pass
            else:
                # Frame dropped or texture invalid: streak broken.
                client.healthy_streak_start = 0.0

        # Amnesty: only fires after a continuous run of delivered frames.
        if (client.healthy_streak_start > 0.0
                and (now - client.healthy_streak_start) >= reset_window):
            with _lifetime_lock:
                if _lifetime_rejects[client.exe] > 0:
                    print(f"[*] {client.exe} continuously delivered frames "
                          f"for {reset_window}s; clearing lifetime rejects "
                          f"({_lifetime_rejects[client.exe]})")
                    _lifetime_rejects[client.exe] = 0
            # Restart streak so the next amnesty requires another full window.
            client.healthy_streak_start = now

        if now < next_tick:
            time.sleep(min(frame_dt / 4, next_tick - now))


def server_loop(analyzer_q: queue.Queue, exe_filter: Optional[str],
                poll_hz: float, connect_timeout: float,
                lifetime_cap: int, reset_window: float):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(b"\x00" + SOCK_PATH)
    sock.listen(4)
    print(f"[*] Listening on abstract socket {SOCK_PATH.decode()} "
          f"(lifetime_cap={lifetime_cap}, reset_window={reset_window}s)")
    while True:
        conn, _ = sock.accept()
        print("[*] Client connected")
        client = Client(conn, exe_filter=exe_filter, lifetime_cap=lifetime_cap)
        threading.Thread(
            target=handle_connection,
            args=(client, analyzer_q, poll_hz, connect_timeout, reset_window),
            daemon=True,
        ).start()


class ClipWriter(threading.Thread):
    def __init__(self, out_dir: str, poll_hz: float):
        super().__init__(daemon=True)
        self.out_dir = out_dir
        self.poll_hz = poll_hz
        self.jobs: queue.Queue = queue.Queue(maxsize=4)

    def submit(self, frames, meta, pix_fmt, ts):
        try:
            self.jobs.put_nowait((frames, meta, pix_fmt, ts))
        except queue.Full:
            print("[!] Writer backlog full; dropping clip")

    def run(self):
        while True:
            try:
                frames, (w, h), pix_fmt, ts = self.jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            out = os.path.join(self.out_dir, f"clip_{int(ts)}.mp4")
            try:
                write_clip(frames, out, self.poll_hz, pix_fmt)
                manifest = {"file": out, "ts": ts, "frames": len(frames),
                            "size": [w, h], "pix_fmt": pix_fmt, "fps": self.poll_hz}
                with open(out + ".json", "w") as fh:
                    json.dump(manifest, fh, indent=2)
                print(f"[+] Clip written: {out} ({len(frames)} frames, {pix_fmt}, {self.poll_hz}fps)")
            except Exception as e:
                print(f"[!] Writer failed: {e}")


def write_clip(frames: list[np.ndarray], out_path: str, fps: float, pix_fmt: str):
    h, w = frames[0].shape[:2]
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", pix_fmt,
        "-s", f"{w}x{h}", "-r", f"{fps:.2f}",
        "-i", "-",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        out_path,
    ]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for f in frames:
            p.stdin.write(f.tobytes())
        p.stdin.close()
        p.wait(timeout=60)
    except (BrokenPipeError, subprocess.TimeoutExpired):
        p.kill()


def analyzer_worker(q: queue.Queue, writer: ClipWriter, poll_hz: float,
                    clip_len: float, motion_thresh: float, refractory: float):
    ring_max = int(clip_len * poll_hz)
    ring: deque = deque(maxlen=ring_max)
    prev_gray = None
    last_clip_ts = 0.0
    collecting: Optional[dict] = None

    while True:
        try:
            ts, frame, (w, h, fmt), exe = q.get(timeout=1.0)
        except queue.Empty:
            continue

        gray = frame[..., :3].mean(axis=2).astype(np.float32)
        if prev_gray is not None and prev_gray.shape == gray.shape:
            motion = float(np.abs(gray - prev_gray).mean()) / 255.0
        else:
            motion = 0.0
        prev_gray = gray

        ring.append((ts, frame))

        if collecting is None and motion > motion_thresh and (ts - last_clip_ts) > refractory:
            last_clip_ts = ts
            collecting = {
                "start_ts": ts,
                "pix_fmt": FORMAT_TO_PIXFMT.get(fmt, "bgra"),
                "size": (w, h),
                "frames": [f for _, f in ring],
                "target": ring_max,
            }
            print(f"[*] Trigger at {ts:.1f} (motion={motion:.3f}); collecting "
                  f"{len(collecting['frames'])}/{ring_max} pre-roll frames")

        elif collecting is not None:
            collecting["frames"].append(frame)
            if len(collecting["frames"]) >= collecting["target"]:
                snap = collecting["frames"]
                print(f"[+] Clip filled: {len(snap)} frames "
                      f"({snap[0].shape[1]}x{snap[0].shape[0]})")
                writer.submit(snap, collecting["size"], collecting["pix_fmt"], collecting["start_ts"])
                collecting = None


def load_config(path: Optional[str]) -> dict:
    """Load configuration dictionary from a TOML file."""
    if not path or not os.path.exists(path):
        return {}
    try:
        try:
            import tomllib
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except ModuleNotFoundError:
            try:
                import tomli as tomllib
                with open(path, "rb") as f:
                    data = tomllib.load(f)
            except ModuleNotFoundError:
                # Basic fallback parser for simple key = value profiles
                data = {}
                with open(path, "r", encoding="utf-8") as f:
                    section = "consumer"
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        if line.startswith("[") and line.endswith("]"):
                            section = line[1:-1].strip()
                            continue
                        if "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip().replace("-", "_")
                            v = v.strip().strip('"').strip("'")
                            if v.lower() in ("true", "false"):
                                val = v.lower() == "true"
                            else:
                                try:
                                    val = int(v) if v.isdigit() else float(v)
                                except ValueError:
                                    val = v
                            data.setdefault(section, {})[k] = val
        return data.get("consumer", data)
    except Exception as e:
        print(f"[!] Warning: Failed loading config from {path}: {e}")
        return {}


def main():
    pre_ap = argparse.ArgumentParser(add_help=False)
    pre_ap.add_argument("--config", "-c", default=None,
                        help="path to TOML configuration profile")
    pre_args, _ = pre_ap.parse_known_args()

    cfg = load_config(pre_args.config) if pre_args.config else {}

    ap = argparse.ArgumentParser(
        description="Standalone OBS-free consumer for obs-vkcapture",
        parents=[pre_ap]
    )
    ap.add_argument("--out", default=cfg.get("out", os.path.expanduser("~/clips")),
                    help="directory to save clips")
    ap.add_argument("--exe", default=cfg.get("exe", None),
                    help="substring filter for client executable path")
    ap.add_argument("--clip-len", type=float, default=float(cfg.get("clip_len", cfg.get("clip-len", 20.0))),
                    help="clip duration in seconds")
    ap.add_argument("--motion", type=float, default=float(cfg.get("motion", 0.1)),
                    help="mean pixel motion threshold to trigger clip")
    ap.add_argument("--refractory", type=float, default=float(cfg.get("refractory", 15.0)),
                    help="minimum seconds between triggers")
    ap.add_argument("--poll-hz", type=float, default=float(cfg.get("poll_hz", cfg.get("poll-hz", 30.0))),
                    help="frame polling frequency in Hz")
    ap.add_argument("--connect-timeout", type=float, default=float(cfg.get("connect_timeout", cfg.get("connect-timeout", 15.0))),
                    help="handshake deadline in seconds")
    ap.add_argument("--lifetime-cap", type=int, default=int(cfg.get("lifetime_cap", cfg.get("lifetime-cap", 10))),
                    help="per-exe cumulative reject ceiling")
    ap.add_argument("--reset-window", type=float, default=float(cfg.get("reset_window", cfg.get("reset-window", 60.0))),
                    help="seconds of continuous frame delivery to fully clear the cap")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    writer = ClipWriter(a.out, a.poll_hz)
    writer.start()

    q: queue.Queue = queue.Queue(maxsize=240)
    threading.Thread(
        target=analyzer_worker,
        args=(q, writer, a.poll_hz, a.clip_len, a.motion, a.refractory),
        daemon=True,
    ).start()
    server_loop(q, a.exe, a.poll_hz, a.connect_timeout,
                a.lifetime_cap, a.reset_window)


if __name__ == "__main__":
    main()
