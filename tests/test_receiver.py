"""OSC boundary, frame ordering, restart, and bounded assembly regressions."""
import struct
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import test_skeleton_diagnostics  # Installs Blender import stubs.
import movin_blender_plugin as movin


def packet(address, args):
    def string(value):
        data = value.encode('utf-8') + b'\0'
        return data + b'\0' * (-len(data) % 4)
    tags = ''.join('s' if isinstance(v, str) else 'i' if type(v) is int else 'f' for v in args)
    return string(address) + string(',' + tags) + b''.join(
        string(v) if t == 's' else struct.pack('>' + t, v) for t, v in zip(tags, args))


def bone(index, parent, name):
    return [index, parent, name, 0., 0., 0., 0., 0., 0., 1., 0., 0., 0., 1., 1., 1., 1.]


def motion(index, count, chunk, total, bones):
    return ['2026-10-01 12:00:00.000', 'MOVINMan', index, count, chunk, total, len(bones)] + [v for b in bones for v in b]


class receiver_tests(unittest.TestCase):
    def setUp(self):
        movin._runtime.reset()
        self.sender = ('127.0.0.1', 11111)

    def receive(self, args, now):
        movin._receive_packet(packet('/MOVIN/Frame', args), self.sender, now)

    def test_reorder_duplicate_and_restart(self):
        for index, now in [(101, 10.), (100, 10.1), (101, 10.2), (0, 10.3)]:
            self.receive(motion(index, 1, 0, 1, [bone(0, -1, 'Root')]), now)
        self.assertEqual(movin._runtime.ready_frames[-1]['frame_idx'], 101)
        self.receive(motion(10, 1, 0, 1, [bone(0, -1, 'Root')]), 11.1)
        self.assertEqual([f['frame_idx'] for f in movin._runtime.ready_frames], [10])

    def test_clock_rollback_does_not_block_new_frames_or_restart(self):
        def send(index, stamp, now):
            args = motion(index, 1, 0, 1, [bone(0, -1, 'Root')])
            args[0] = stamp
            self.receive(args, now)

        send(100, '2026-10-01 12:00:00.000', 10.)
        send(101, '2026-10-01 11:00:00.000', 10.1)
        self.assertEqual(movin._runtime.ready_frames[-1]['frame_idx'], 101)
        send(0, '2026-10-01 10:00:00.000', 10.2)
        self.assertEqual(movin._runtime.ready_frames[-1]['frame_idx'], 101)
        send(1, '2026-10-01 10:00:01.000', 11.2)
        self.assertEqual([f['frame_idx'] for f in movin._runtime.ready_frames], [1])

    def test_motion_queue_keeps_only_two_newest_frames_during_stall(self):
        for index in range(1000):
            self.receive(motion(index, 1, 0, 1, [bone(0, -1, 'Root')]), 10. + index / 60)
            self.assertLessEqual(len(movin._runtime.ready_frames), 2)
        queue = movin._runtime.ready_frames
        self.assertEqual([queue.popleft()['frame_idx'], queue.popleft()['frame_idx']], [998, 999])

    def test_source_and_character_changes_clear_queued_motion(self):
        for index in (1, 2):
            self.receive(motion(index, 1, 0, 1, [bone(0, -1, 'Root')]), 10.)
        movin._receive_packet(packet('/MOVIN/Frame', motion(0, 1, 0, 1, [bone(0, -1, 'Root')])),
                              ('127.0.0.1', 22222), 12.1)
        self.assertEqual([f['frame_idx'] for f in movin._runtime.ready_frames], [0])
        changed = motion(1, 1, 0, 1, [bone(0, -1, 'NewRoot')])
        changed[1] = 'NewCharacter'
        movin._receive_packet(packet('/MOVIN/Frame', changed), ('127.0.0.1', 22222), 12.2)
        self.assertEqual([f['actor'] for f in movin._runtime.ready_frames], ['NewCharacter'])

    def test_cloud_queue_still_keeps_only_latest_frame(self):
        for index in range(10):
            movin._receive_packet(packet('/MOVIN/PointCloud', [index, 1, 0, 1, 1, 0., 0., 0.]),
                                  self.sender, 10. + index / 60)
        self.assertEqual([f['frame_idx'] for f in movin._runtime.ready_pointclouds], [9])

    def test_interleaved_frames_keep_newer_partials(self):
        for index, chunk in [(2, 0), (3, 0), (2, 1), (3, 1)]:
            self.receive(motion(index, 2, chunk, 2, [bone(chunk, chunk-1, str(chunk))]), 10.)
        self.assertEqual(movin._runtime.ready_frames[-1]['frame_idx'], 3)

    def test_sources_do_not_mix_or_steal_active_stream(self):
        a = motion(1, 2, 0, 2, [bone(0, -1, 'Root')])
        b = motion(1, 2, 1, 2, [bone(1, 0, 'Hips')])
        self.receive(a, 10.)
        movin._receive_packet(packet('/MOVIN/Frame', b), ('127.0.0.1', 22222), 10.)
        self.assertFalse(movin._runtime.ready_frames)
        self.receive(b, 10.1)
        movin._receive_packet(packet('/MOVIN/Frame', motion(2, 1, 0, 1, [bone(0, -1, 'Other')])), ('127.0.0.1', 22222), 10.2)
        self.assertEqual(movin._runtime.motion.sender, self.sender)

    def test_invalid_counts_types_indices_and_transforms(self):
        valid = motion(1, 1, 0, 1, [bone(0, -1, 'Root')])
        for index, value in [(2, -1), (3, 0), (4, 1), (5, 999), (6, 0), (7, -1), (8, 0), (10, float('nan')), (10, 1)]:
            args = valid.copy()
            args[index] = value
            with self.subTest(index=index, value=value):
                with self.assertRaises(ValueError):
                    self.receive(args, 10.)
        with self.assertRaises(ValueError):
            self.receive(valid + [0], 10.)
        self.assertFalse(movin._runtime.ready_frames)

    def test_conflicting_metadata_and_duplicate_bones(self):
        self.receive(motion(1, 2, 0, 2, [bone(0, -1, 'Root')]), 10.)
        changed = motion(1, 2, 1, 2, [bone(1, 0, 'Hips')])
        changed[0] = 'different timestamp'
        with self.assertRaises(ValueError):
            self.receive(changed, 10.)
        for bones in [[bone(0, -1, 'Root'), bone(0, -1, 'Hips')],
                      [bone(0, -1, 'Root'), bone(1, 0, 'Root')],
                      [bone(0, -1, 'Root'), bone(2, 1, 'Hips')]]:
            with self.assertRaises(ValueError):
                self.receive(motion(2, 1, 0, 2, bones), 10.)

    def test_partial_buffers_expire_under_continuous_traffic_and_are_bounded(self):
        for i in range(2000):
            self.receive(motion(i, 2, 0, 2, [bone(0, -1, 'Root')]), 10. + i / 1000.)
        buffers = movin._runtime.frame_buffers
        self.assertLessEqual(len(buffers), 8)
        self.assertTrue(all(11.999 - b['time'] < .5 for b in buffers.values()))
        movin._runtime.motion.expire(13.)
        self.assertFalse(buffers)

    def test_cloud_validation_reordering_and_empty_frame(self):
        def send(args, now):
            movin._receive_packet(packet('/MOVIN/PointCloud', args), self.sender, now)
        send([10, 1, 0, 1, 1, 0., 0., 0.], 10.)
        send([9, 1, 0, 1, 1, 0., 0., 0.], 10.1)
        self.assertEqual(movin._runtime.cloud.last_frame, 10)
        with self.assertRaises(ValueError):
            send([11, 999, 0, 1, 1, 0., 0., 0.], 10.2)
        send([12, 0, 0, 1, 0], 10.3)
        self.assertEqual(movin._runtime.ready_pointclouds[-1]['points'], [])

    def test_status_request_and_validation_commands(self):
        movin._receive_packet(packet('/MOVIN/Blender/Status/Request', ['a'*32, 39581]), self.sender, 10.)
        self.assertEqual(movin._runtime.status_request, (self.sender, 39581, 'a'*32))
        with self.assertRaises(ValueError):
            movin._receive_packet(packet('/MOVIN/Blender/Status/Request', ['a'*32, 65536]), self.sender, 10.)
        movin._receive_packet(packet('/MOVIN/StreamValidation/Begin', ['ignored', 'Blender', 1, 'unused']), self.sender, 10.)
        self.assertFalse(hasattr(movin, '_validation_logger'))

    def test_osc_invalid_utf8_and_trailing_bytes(self):
        with self.assertRaises(ValueError):
            movin._OscReader(b'/\xff\0\0,s\0\0a\0\0\0').read_message()
        with self.assertRaises(ValueError):
            movin._OscReader(packet('/test', [1]) + b'\0\0\0\0').read_message()

    def test_numeric_payload_preserves_types_and_rejects_truncation(self):
        values = [-2147483648, 2147483647, 0, -1.25, 2.5, 0.0]
        data = packet('/test', values)
        address, decoded = movin._OscReader(data).read_message()
        self.assertEqual(address, '/test')
        self.assertEqual(decoded, values)
        self.assertEqual([type(v) for v in decoded], [type(v) for v in values])
        for size in range(1, 5):
            with self.assertRaises(ValueError):
                movin._OscReader(data[:-size]).read_message()
        with self.assertRaises(ValueError):
            movin._OscReader(data.replace(b',iiifff', b',iiifdf')).read_message()

    def test_cloud_duplicate_chunks_do_not_inflate_size(self):
        def send(args):
            movin._receive_packet(packet('/MOVIN/PointCloud', args), self.sender, 10.)
        first = [1, 2, 0, 2, 1, 1., 2., 3.]
        send(first)
        send(first)
        send([1, 2, 1, 2, 1, 4., 5., 6.])
        self.assertEqual(movin._runtime.ready_pointclouds[-1]['points'], [(1., 2., 3.), (4., 5., 6.)])
        send([2, 2, 0, 2, 2, 1., 2., 3., 4., 5., 6.])
        with self.assertRaises(ValueError):
            send([2, 2, 1, 2, 1, 7., 8., 9.])
        self.assertEqual(movin._runtime.cloud.last_frame, 1)

    def test_cloud_numeric_values_remain_validated(self):
        for value in (float('nan'), float('inf'), -float('inf'), 1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    movin._receive_packet(packet('/MOVIN/PointCloud', [1, 1, 0, 1, 1, value, 0., 0.]),
                                          self.sender, 10.)
        self.assertFalse(movin._runtime.ready_pointclouds)


class receiver_status_tests(unittest.TestCase):
    def setUp(self):
        movin._runtime.reset()
        self.token = 'a' * 32
        self.received = threading.Event()
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender.bind(('127.0.0.1', 0))
        self.addCleanup(self.sender.close)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.settimeout(0.01)
        self.listen_address = self.listener.getsockname()
        self.addCleanup(self.listener.close)
        scene = SimpleNamespace(movin_props=SimpleNamespace(armature_name=''))
        data = SimpleNamespace(scenes={'Scene': scene}, objects={})
        timers = SimpleNamespace(is_registered=lambda callback: False)
        receive_packet = movin._receive_packet

        def receive(data, sender, now):
            receive_packet(data, sender, now)
            self.received.set()

        for p in (patch.object(movin.bpy, 'data', data),
                  patch.object(movin.bpy.app, 'timers', timers),
                  patch.object(movin, '_apply_latest_stream_data', return_value=1.0 / 120.0),
                  patch.object(movin, '_receive_packet', side_effect=receive)):
            p.start()
            self.addCleanup(p.stop)
        movin._runtime.sock = self.listener
        movin._runtime.scene_name = 'Scene'
        movin._runtime.running = True
        movin._runtime.thread = threading.Thread(target=movin._udp_server_loop, args=(self.listener,), daemon=True)
        movin._runtime.thread.start()
        self.addCleanup(movin._stop_receiver)

    def request(self, port):
        self.received.clear()
        self.sender.sendto(packet('/MOVIN/Blender/Status/Request', [self.token, port]), self.listen_address)
        self.assertTrue(self.received.wait(1.0), 'Status request was not received')

    def assert_motion_received(self):
        self.received.clear()
        self.sender.sendto(packet('/MOVIN/Frame', motion(0, 1, 0, 1, [bone(0, -1, 'Root')])),
                           self.listen_address)
        self.assertTrue(self.received.wait(1.0), 'Motion receiver stopped after a status reply')
        self.assertEqual(movin._runtime.ready_frames[-1]['frame_idx'], 0)
        self.assertTrue(movin._runtime.running)
        self.assertTrue(movin._runtime.thread.is_alive())

    def test_status_reply_uses_separate_socket_and_preserves_payload(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reply:
            reply.bind(('127.0.0.1', 0))
            reply.settimeout(1.0)
            self.request(reply.getsockname()[1])
            self.assertIsNotNone(movin._timer_tick())
            data, sender = reply.recvfrom(65535)
        self.assertNotEqual(sender[1], self.listen_address[1])
        address, args = movin._OscReader(data).read_message()
        self.assertEqual(address, '/MOVIN/Blender/Status')
        self.assertEqual(args, [self.token, 1, '', 0, 0, 0, 0., 0., 0., -1., -1., 0, 0, ''])
        self.assert_motion_received()

    def test_closed_status_port_allows_sender_restart_and_later_reply(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as closed:
            closed.bind(('127.0.0.1', 0))
            port = closed.getsockname()[1]
        self.request(port)
        self.assertIsNotNone(movin._timer_tick())
        self.sender.close()
        # Let the closed port's ICMP reply arrive and the status rate limit expire.
        self.received.clear()
        self.received.wait(0.3)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sender.bind(('127.0.0.1', 0))
        self.addCleanup(self.sender.close)
        self.assert_motion_received()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as reply:
            reply.bind(('127.0.0.1', 0))
            reply.settimeout(1.0)
            self.request(reply.getsockname()[1])
            self.assertIsNotNone(movin._timer_tick())
            data, _ = reply.recvfrom(65535)
        self.assertEqual(movin._OscReader(data).read_message()[1][0], self.token)

    def test_status_socket_creation_failure_is_reported_and_motion_continues(self):
        self.request(self.sender.getsockname()[1])
        error = OSError('Status socket unavailable')
        with patch.object(movin.socket, 'socket', side_effect=error), patch('builtins.print') as report:
            self.assertIsNotNone(movin._timer_tick())
        report.assert_any_call('[MOVIN Live] Status reply failed:', error)
        self.assertIs(movin._runtime.sock, self.listener)
        self.assertIsNone(movin._runtime.status_request)
        self.assertGreater(movin._runtime.status_sent, 0.0)
        self.assertEqual(movin._runtime.socket_error, '')
        self.assert_motion_received()

    def test_status_send_failure_closes_reply_socket_and_motion_continues(self):
        self.request(self.sender.getsockname()[1])
        error = OSError('Status destination unavailable')
        reply = MagicMock()
        reply.__enter__.return_value = reply
        reply.sendto.side_effect = error
        with patch.object(movin.socket, 'socket', return_value=reply), patch('builtins.print') as report:
            self.assertIsNotNone(movin._timer_tick())
        report.assert_any_call('[MOVIN Live] Status reply failed:', error)
        reply.__exit__.assert_called_once()
        self.assertIs(movin._runtime.sock, self.listener)
        self.assert_motion_received()


if __name__ == '__main__':
    unittest.main(verbosity=2)
