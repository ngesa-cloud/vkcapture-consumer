import os
import queue
import socket
import struct
import threading
import time
import unittest

from vkcapture_consumer.consumer import (
    Client,
    TextureState,
    parse_client_data,
    parse_texture_data,
    build_control,
    handle_connection,
    load_config,
    CLIENT_DATA_FMT,
    TEXTURE_DATA_FMT,
    CONTROL_DATA_FMT,
    CAPTURE_CLIENT_DATA_SIZE,
    CAPTURE_TEXTURE_DATA_SIZE,
    CAPTURE_CONTROL_DATA_SIZE,
    CAPTURE_CLIENT_DATA_TYPE,
    CAPTURE_TEXTURE_DATA_TYPE,
    DRM_FORMAT_ARGB8888,
    FORMAT_TO_PIXFMT,
    MAX_REJECTS,
    _lifetime_rejects,
    _lifetime_lock,
)


class TestVkCaptureProtocol(unittest.TestCase):
    def test_load_config(self):
        profile_path = os.path.join(os.path.dirname(__file__), "..", "profiles", "gtavi.toml")
        cfg = load_config(profile_path)
        self.assertEqual(cfg.get("exe"), "gtavi.exe")
        self.assertEqual(cfg.get("clip_len"), 15.0)
        self.assertEqual(cfg.get("poll_hz"), 30.0)

    def test_struct_sizes(self):
        self.assertEqual(struct.calcsize(CLIENT_DATA_FMT), CAPTURE_CLIENT_DATA_SIZE)
        self.assertEqual(struct.calcsize(TEXTURE_DATA_FMT), CAPTURE_TEXTURE_DATA_SIZE)
        self.assertEqual(struct.calcsize(CONTROL_DATA_FMT), CAPTURE_CONTROL_DATA_SIZE)

    def test_parse_client_data(self):
        exe_name = b"/usr/bin/gtavi.exe\x00"
        padded_exe = exe_name + b"\x00" * (48 - len(exe_name))
        buf = struct.pack(CLIENT_DATA_FMT, CAPTURE_CLIENT_DATA_TYPE, padded_exe, b"\x00" * 79)
        msg_type, exe = parse_client_data(buf)
        self.assertEqual(msg_type, CAPTURE_CLIENT_DATA_TYPE)
        self.assertEqual(exe, "/usr/bin/gtavi.exe")

    def test_build_control(self):
        ctrl = build_control(capturing=True, no_modifiers=True, linear=True, map_host=True)
        self.assertEqual(len(ctrl), CAPTURE_CONTROL_DATA_SIZE)
        c, nm, l, mh, uuid, pad = struct.unpack(CONTROL_DATA_FMT, ctrl)
        self.assertEqual(c, 1)
        self.assertEqual(nm, 1)
        self.assertEqual(l, 1)
        self.assertEqual(mh, 1)

    def test_client_retry_ladder(self):
        server_sock, client_sock = socket.socketpair()
        try:
            test_exe = "test_game.exe"
            with _lifetime_lock:
                _lifetime_rejects[test_exe] = 0

            client = Client(server_sock, lifetime_cap=5)
            client.exe = test_exe

            # Reject 1: escalates no_modifiers=1
            ret = client.request_retry()
            self.assertTrue(ret)
            self.assertEqual(client.rejects, 1)
            ctrl = client_sock.recv(CAPTURE_CONTROL_DATA_SIZE)
            c, nm, l, mh, _, _ = struct.unpack(CONTROL_DATA_FMT, ctrl)
            self.assertTrue(c == 1 and nm == 1 and l == 1 and mh == 1)

            # Reject 2: stop/init cycle
            ret = client.request_retry()
            self.assertTrue(ret)
            self.assertEqual(client.rejects, 2)
            ctrl_stop = client_sock.recv(CAPTURE_CONTROL_DATA_SIZE)
            ctrl_start = client_sock.recv(CAPTURE_CONTROL_DATA_SIZE)
            c0, _, _, _, _, _ = struct.unpack(CONTROL_DATA_FMT, ctrl_stop)
            c1, _, _, _, _, _ = struct.unpack(CONTROL_DATA_FMT, ctrl_start)
            self.assertEqual(c0, 0)
            self.assertEqual(c1, 1)

            # Reject 3: max rejects exceeded
            ret = client.request_retry()
            self.assertFalse(ret)
            self.assertEqual(client.rejects, 3)

            # Test lifetime cap enforcement
            with _lifetime_lock:
                _lifetime_rejects[test_exe] = 6
            ret = client.request_retry()
            self.assertFalse(ret)
            self.assertFalse(client.running)
        finally:
            server_sock.close()
            client_sock.close()

    def test_streak_disarm_on_reject(self):
        server_sock, _ = socket.socketpair()
        try:
            client = Client(server_sock)
            client.healthy_streak_start = 12345.0
            client.request_retry()
            self.assertEqual(client.healthy_streak_start, 0.0)
        finally:
            server_sock.close()
            _.close()

    def test_socket_handshake_and_nfd_rejection(self):
        """Integration test: spins handle_connection and tests handshake + nfd mismatch."""
        server_sock, client_sock = socket.socketpair()
        analyzer_q = queue.Queue()
        client = Client(server_sock, lifetime_cap=10)

        t = threading.Thread(
            target=handle_connection,
            args=(client, analyzer_q, 30.0, 5.0, 60.0),
            daemon=True,
        )
        t.start()

        try:
            # 1. Send client greeting
            exe_name = b"synthetic_game\x00"
            padded_exe = exe_name + b"\x00" * (48 - len(exe_name))
            greeting = struct.pack(CLIENT_DATA_FMT, CAPTURE_CLIENT_DATA_TYPE, padded_exe, b"\x00" * 79)
            client_sock.sendall(greeting)

            # 2. Receive server control response
            ctrl = client_sock.recv(CAPTURE_CONTROL_DATA_SIZE)
            self.assertEqual(len(ctrl), CAPTURE_CONTROL_DATA_SIZE)
            c, nm, l, mh, _, _ = struct.unpack(CONTROL_DATA_FMT, ctrl)
            self.assertTrue(c == 1 and l == 1 and mh == 1)

            # 3. Send bogus texture header claiming nfd=2 (multi-plane), with a dummy fd
            pipe_r, pipe_w = os.pipe()
            try:
                tex_hdr = struct.pack(
                    TEXTURE_DATA_FMT,
                    CAPTURE_TEXTURE_DATA_TYPE,
                    2,  # nfd = 2 (multi-plane trigger)
                    1920, 1080, DRM_FORMAT_ARGB8888,
                    7680, 0, 0, 0,
                    0, 0, 0, 0,
                    0, 0, 0, 0,
                    b"\x00" * 65
                )
                anc = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", pipe_r))]
                client_sock.sendmsg([tex_hdr], anc)

                # 4. Server detects FD mismatch and drops connection or tears down
                time.sleep(0.1)
                self.assertTrue(not client.accepted or client.rejects > 0 or not client.running)
            finally:
                os.close(pipe_r)
                os.close(pipe_w)

        finally:
            client.running = False
            client_sock.close()
            t.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
