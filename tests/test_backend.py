import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'root/usr/lib/e5-esim'))
import bridge
import job

loader = importlib.machinery.SourceFileLoader('esim_rpc', str(BASE / 'root/usr/libexec/rpcd/e5-esim'))
spec = importlib.util.spec_from_loader(loader.name, loader)
rpc = importlib.util.module_from_spec(spec)
loader.exec_module(rpc)


class Ring:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.commands = []

    def command(self, body, timeout):
        self.commands.append(body)
        return next(self.replies)


SE0, SE1 = rpc.SE_AIDS


def chip_data(label):
    return {'eidValue': 'eid-' + label, 'EUICCInfo2': {}}


def chip_result(label):
    return {'ok': True, 'data': chip_data(label), 'error': None, 'detail': None}


class SnapshotRun:
    """Answers chip reads per AID and records the selected-AID traffic."""

    def __init__(self, chips):
        self.chips = chips
        self.calls = []

    def __call__(self, slot, argv, aid=''):
        self.calls.append((tuple(argv), aid))
        if argv == ['chip', 'info']:
            return self.chips[aid]
        return {'ok': True, 'data': [], 'error': None, 'detail': None}

    def aids_for(self, command):
        return [aid for argv, aid in self.calls if argv == command]


class BridgeFlows(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.TemporaryDirectory()
        self.addCleanup(self.state.cleanup)
        patcher = patch.object(job, 'CANCEL', str(Path(self.state.name) / 'cancel'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_write_progress_counts_accepted_bpp_bytes(self):
        events = []
        writer = bridge.WriteProgress(lambda message, data: events.append((message, data)))
        writer.accept('81E2110006BF3607AABBCC', '+CGLA: 4,9000')
        self.assertEqual(events, [])
        writer.active = True
        writer.accept('81E2110006BF3607AABBCC', '+CGLA: 4,6A80')
        self.assertEqual(events, [])
        writer.accept('81E2110006BF3607AABBCC', '+CGLA: 4,9000')
        self.assertEqual(events[-1][1], {'written': 6, 'total': 10, 'percent': 60})
        writer.accept('81C0000010', '+CGLA: 4,9000')
        self.assertEqual(len(events), 1)
        writer.accept('81E2910004AABBCCDD', '+CGLA: 4,610A')
        self.assertEqual(events[-1][1], {'written': 10, 'total': 10, 'percent': 100})

    def test_write_progress_reads_long_ber_length(self):
        events = []
        writer = bridge.WriteProgress(lambda message, data: events.append(data))
        writer.active = True
        writer.accept('81E2110008BF36820100AABBCC', '+CGLA: 4,9000')
        self.assertEqual(events, [{'written': 8, 'total': 261, 'percent': 3}])

    def test_retried_write_is_counted_once(self):
        events = []
        writer = bridge.WriteProgress(lambda message, data: events.append(data))
        writer.active = True
        ring = Ring(['+CME ERROR: 10', 'OK', '+CCID: 123\r\nOK', '+CCHO: 2\r\nOK', '+CGLA: 4,9000'])
        transport = bridge.Bridge(-1, ring, writer.accept)
        transport.aid, transport.channel, transport.open = 'A000', '1', True
        transport.say = lambda line: None
        with patch.object(bridge.time, 'sleep'):
            transport.request('1,22,"81E2110006BF3607AABBCC"')
        self.assertEqual(events, [{'written': 6, 'total': 10, 'percent': 60}])

    def test_open_sweeps_only_after_failure(self):
        ring = Ring(['+CCHO: 1\r\nOK'])
        transport = bridge.Bridge(-1, ring)
        transport.aid = 'A000'
        self.assertEqual(transport.open_channel(), '1')
        self.assertEqual(ring.commands, ['+CCHO="A000"'])
        ring = Ring(['+CME ERROR: 22', 'OK', 'OK', 'OK', '+CCHO: 2\r\nOK'])
        transport = bridge.Bridge(-1, ring)
        transport.aid = 'A000'
        self.assertEqual(transport.open_channel(), '2')
        self.assertEqual(ring.commands, ['+CCHO="A000"'] +
                         ['+CSIM=10,"0070800%d00"' % n for n in (1, 2, 3)] + ['+CCHO="A000"'])

    def test_transmit_recovers_once_and_uses_reopened_channel(self):
        ring = Ring(['+CME ERROR: 10', 'OK', '+CCID: 123\r\nOK',
                     '+CCHO: 3\r\nOK', '+CME ERROR: 10'])
        transport = bridge.Bridge(-1, ring)
        transport.aid, transport.channel, transport.open = 'A000', '1', True
        sent = []
        transport.say = sent.append
        with patch.object(bridge.time, 'sleep'):
            transport.request('1,4,"8100"')
        self.assertEqual(ring.commands, ['+CGLA=1,4,"8100"', '+SFUN=2', '+CCID',
                                         '+CCHO="A000"', '+CGLA=3,4,"8100"'])
        self.assertEqual(sent, ['+CME ERROR: 10', 'ERROR'])

    def test_http_wait_can_be_cancelled_without_at_traffic(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'lpac'
            executable.write_text('#!/usr/bin/env python3\nimport signal,time\nsignal.signal(signal.SIGINT, lambda *_: exit(0))\nwhile True: time.sleep(.01)\n')
            executable.chmod(0o755)
            import time
            started = time.monotonic()
            with patch.object(bridge, 'LPAC', str(executable)), patch.object(bridge, 'open_device', return_value=None):
                result = bridge.run_lpac(1, ['profile', 'download'], cancel=lambda: time.monotonic() - started > .2)
            self.assertFalse(result['ok'])
            self.assertLess(time.monotonic() - started, 2)

    def test_ignored_sigint_returns_cancelled_after_short_grace(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'ready'
            executable = Path(directory) / 'lpac'
            executable.write_text('#!/usr/bin/env python3\nimport os,signal,time\n'
                                  'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                                  "open(os.environ['E5_READY'], 'w').close()\n"
                                  'while True: time.sleep(.01)\n')
            executable.chmod(0o755)
            started = time.monotonic()
            with patch.object(bridge, 'LPAC', str(executable)), \
                    patch.object(bridge, 'open_device', return_value=None), \
                    patch.dict(os.environ, {'E5_READY': str(marker)}):
                result = bridge.run_lpac(1, ['profile', 'download'], cancel=marker.exists)
            elapsed = time.monotonic() - started
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'cancelled')
        self.assertLessEqual(bridge.CANCEL_GRACE, 5)
        self.assertGreaterEqual(elapsed, bridge.CANCEL_GRACE - .5)
        self.assertLess(elapsed, bridge.CANCEL_GRACE + 2)

    def test_cancel_waits_for_active_at_exchange(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'lpac'
            marker = Path(directory) / 'ready'
            executable.write_text(
                '#!/usr/bin/env python3\n'
                'import os,signal,time\n'
                'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                "with open(os.environ['LPAC_APDU_AT_DEVICE'], 'w', buffering=1) as device:\n"
                "    device.write('AT+CCID\\r\\n')\n"
                "open(os.environ['E5_READY'], 'w').close()\n"
                'while True: time.sleep(.01)\n')
            executable.chmod(0o755)
            master, slave = pty.openpty()
            try:
                started = time.monotonic()
                with patch.object(bridge, 'LPAC', str(executable)), \
                        patch.object(bridge, 'open_device', return_value=slave), \
                        patch.object(bridge, 'CHANNEL_TIMEOUT', .6), \
                        patch.object(bridge, 'CANCEL_GRACE', .2), \
                        patch.dict(os.environ, {'E5_READY': str(marker)}):
                    result = bridge.run_lpac(1, ['chip', 'info'], cancel=marker.exists)
                elapsed = time.monotonic() - started
            finally:
                os.close(master)
        self.assertEqual(result['error'], 'cancelled')
        self.assertGreaterEqual(elapsed, .55)
        self.assertLess(elapsed, 2)

    def test_cancel_covers_initial_notification_read(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            log = work / 'calls'
            executable = work / 'lpac'
            executable.write_text(
                '#!/usr/bin/env python3\n'
                'import os,signal,sys,time\n'
                'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                "with open(os.environ['E5_TEST_LOG'], 'a') as output:\n"
                "    output.write(' '.join(sys.argv[1:]) + '\\n')\n"
                "with open(os.environ['E5_CANCEL'], 'w') as output:\n"
                "    output.write(os.environ['E5_CANCEL_PID'])\n"
                'while True: time.sleep(.01)\n')
            executable.chmod(0o755)
            with patch.object(bridge, 'LPAC', str(executable)), \
                    patch.object(bridge, 'open_device', return_value=None), \
                    patch.object(bridge, 'CANCEL_GRACE', .2), \
                    patch.dict(os.environ, {'E5_TEST_LOG': str(log), 'E5_CANCEL': job.CANCEL,
                                            'E5_CANCEL_PID': str(os.getpid())}):
                result = job.download(1, 'LPA:1$x', '')
            calls = log.read_text().splitlines()
        self.assertEqual(result['error'], 'cancelled')
        self.assertEqual(calls, ['notification list'])

    def test_preview_stops_on_cancel_request(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            fifo = work / 'job.in'
            os.mkfifo(fifo)
            Path(job.CANCEL).write_text(str(os.getpid()))
            started = time.monotonic()
            with patch.object(job, 'FIFO', str(fifo)), \
                    patch.object(job, 'STATUS', str(work / 'job.json')), \
                    patch.object(job, 'publish'):
                answer = job.preview()
            elapsed = time.monotonic() - started
        self.assertFalse(answer)
        self.assertLess(elapsed, 1)

    def test_job_cancel_writes_pid_request_without_signals_and_refuses_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            pidfile, status, cancel = work / 'job.pid', work / 'job.json', work / 'cancel'
            pidfile.write_text(str(os.getpid()))
            status.write_text(json.dumps({'type': 'download', 'state': 'running'}))
            with patch.multiple(rpc, PIDFILE=str(pidfile), STATUS=str(status), CANCEL=str(cancel)), \
                    patch.object(rpc, 'job_status', return_value={'running': True, 'type': 'download', 'state': 'running'}), \
                    patch.object(rpc.os, 'kill', side_effect=AssertionError('must not signal')):
                result = rpc.job_cancel({})
            self.assertTrue(result['ok'])
            self.assertEqual(cancel.read_text(), str(os.getpid()))

            cancel.unlink()
            with patch.multiple(rpc, PIDFILE=str(pidfile), STATUS=str(status), CANCEL=str(cancel)), \
                    patch.object(rpc, 'job_status', return_value={'running': True, 'type': 'operation', 'state': 'running'}):
                result = rpc.job_cancel({})
            self.assertFalse(result['ok'])
            self.assertEqual(result['error'], 'cancel')
            self.assertEqual(result['detail'], 'no active download')
            self.assertFalse(cancel.exists())

    def test_cancel_request_before_signal_handler_is_pid_scoped(self):
        Path(job.CANCEL).write_text(str(os.getpid()))
        self.assertTrue(job.cancel_requested())
        Path(job.CANCEL).write_text(str(os.getpid() + 1))
        self.assertFalse(job.cancel_requested())

    def test_cancel_preserves_already_committed_success(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'lpac'
            marker = Path(directory) / 'ready'
            executable.write_text(
                '#!/usr/bin/env python3\n'
                'import json,os,signal,time\n'
                'signal.signal(signal.SIGINT, signal.SIG_IGN)\n'
                "print(json.dumps({'type': 'lpa', 'payload': {'code': 0, 'data': {'ok': True}, 'message': ''}}), flush=True)\n"
                "open(os.environ['E5_READY'], 'w').close()\n"
                'while True: time.sleep(.01)\n')
            executable.chmod(0o755)
            started = time.monotonic()
            with patch.object(bridge, 'LPAC', str(executable)), \
                    patch.object(bridge, 'open_device', return_value=None), \
                    patch.object(bridge, 'CANCEL_GRACE', .2), \
                    patch.dict(os.environ, {'E5_READY': str(marker)}):
                result = bridge.run_lpac(1, ['profile', 'download'], cancel=marker.exists)
            elapsed = time.monotonic() - started
        self.assertTrue(result['ok'])
        self.assertEqual(result['data'], {'ok': True})
        self.assertGreaterEqual(elapsed, .1)
        self.assertLess(elapsed, 2)

    def test_profiles_obeys_job_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(rpc, 'STATE_DIR', directory), patch.object(rpc, 'LOCK', str(Path(directory) / 'lock')):
                fd = rpc.acquire()
                try:
                    with patch.object(bridge, 'run_lpac') as run:
                        with self.assertRaises(rpc.Busy):
                            rpc.dispatch('profiles', {'slot': 1})
                        run.assert_not_called()
                finally:
                    os.close(fd)

    def test_snapshot_stops_on_chip_failure(self):
        error = bridge.failed()
        with patch.object(bridge, 'probe_estk') as probe, patch.object(bridge, 'run_lpac', return_value=error) as run:
            result = rpc.snapshot({'slot': 1})
        self.assertIsNone(result['profiles'])
        self.assertEqual(run.call_args_list[0].args, (1, ['chip', 'info']))
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result['aid'], '')
        probe.assert_not_called()

    def test_snapshot_scan_selects_first_available_se(self):
        standard = chip_result('std')
        cases = [
            ('both', standard, chip_result('se0'), chip_result('se1'), 3, SE0, 'se0'),
            ('se1_only', standard, bridge.failed(), chip_result('se1'), 2, SE1, 'se1'),
            ('none', standard, bridge.failed(), bridge.failed(), 0, '', 'std'),
        ]
        for label, std, se0, se1, ses, aid, chip in cases:
            with self.subTest(label=label):
                runner = SnapshotRun({'': std, SE0: se0, SE1: se1})
                with patch.object(bridge, 'run_lpac', side_effect=runner), \
                        patch.object(bridge, 'probe_estk', return_value=True):
                    result = rpc.snapshot({'slot': 1})
                self.assertTrue(result['ok'])
                self.assertEqual(result['ses'], ses)
                self.assertEqual(result['aid'], aid)
                self.assertEqual(result['chip']['data'], chip_data(chip))
                self.assertEqual(runner.aids_for(('chip', 'info')), ['', SE0, SE1])
                self.assertEqual(runner.aids_for(('profile', 'list')), [aid])
                self.assertEqual(runner.aids_for(('notification', 'list')), [aid])

    def test_snapshot_vendor_failure_keeps_standard_chip(self):
        standard = chip_result('std')
        runner = SnapshotRun({'': standard})
        with patch.object(bridge, 'run_lpac', side_effect=runner), \
                patch.object(bridge, 'probe_estk', return_value=False):
            result = rpc.snapshot({'slot': 0})
        self.assertEqual((result['ses'], result['aid']), (0, ''))
        self.assertEqual(result['chip'], standard)
        self.assertEqual(runner.aids_for(('chip', 'info')), [''])
        self.assertEqual(runner.aids_for(('profile', 'list')), [''])

    def test_snapshot_scan_false_skips_vendor_probe(self):
        runner = SnapshotRun({'': chip_result('std')})
        with patch.object(bridge, 'run_lpac', side_effect=runner), \
                patch.object(bridge, 'probe_estk') as probe:
            result = rpc.snapshot({'slot': 0, 'scan': False})
        probe.assert_not_called()
        self.assertNotIn('ses', result)
        self.assertEqual(runner.aids_for(('chip', 'info')), [''])
        self.assertEqual(runner.aids_for(('profile', 'list')), [''])

    def test_snapshot_explicit_aid_is_kept_and_reused(self):
        runner = SnapshotRun({'': chip_result('std'), SE0: chip_result('se0'), SE1: chip_result('se1')})
        with patch.object(bridge, 'run_lpac', side_effect=runner), \
                patch.object(bridge, 'probe_estk', return_value=True):
            result = rpc.snapshot({'slot': 0, 'aid': SE1})
        self.assertEqual(result['aid'], SE1)
        self.assertEqual(result['chip']['data'], chip_data('se1'))
        self.assertEqual(result['ses'], 3)
        self.assertEqual(runner.aids_for(('chip', 'info')), [SE1, SE0])
        self.assertEqual(runner.aids_for(('profile', 'list')), [SE1])

    def test_aid_and_scan_are_validated_at_the_boundary(self):
        for params in ({'slot': 0, 'aid': 'A0'}, {'slot': 0, 'aid': None},
                       {'slot': 0, 'scan': 1}, {'slot': 0, 'scan': 'true'}):
            with self.subTest(params=params), patch.object(bridge, 'run_lpac') as run:
                with self.assertRaises(rpc.Invalid):
                    rpc.snapshot(params)
                run.assert_not_called()
        with patch.object(bridge, 'run_lpac') as run:
            with self.assertRaises(rpc.Invalid):
                rpc.profiles({'slot': 0, 'aid': 'A0'})
            run.assert_not_called()

    def test_switch_refreshes_then_retries_list_once(self):
        requests = []
        replies = iter([{'ok': True}, bridge.failed(),
                        {'ok': True, 'data': [{'iccid': '123', 'profileState': 'enabled'}]}])
        def run(slot, argv, aid='', **kwargs):
            requests.append(argv)
            return next(replies)
        with patch.object(bridge, 'run_lpac', side_effect=run), patch.object(bridge, 'sim_refresh', return_value=True) as refresh, patch.object(job.time, 'sleep') as sleep, patch.object(job, 'state', {}):
            result = job.operation(1, {'action': 'switch', 'iccid': '123', 'enabled': True})
        self.assertTrue(result['ok'])
        self.assertEqual(requests, [['profile', 'enable', '123'], ['profile', 'list'], ['profile', 'list']])
        refresh.assert_called_once_with(1)
        self.assertEqual([call.args for call in sleep.call_args_list], [(3,), (3,)])

    def test_switch_stops_at_six_reads_and_flags_pending_state(self):
        stale = {'ok': True, 'data': [{'iccid': '123', 'profileState': 'disabled'}]}
        state = {}
        with patch.object(bridge, 'run_lpac', side_effect=[{'ok': True}] + [stale] * 6) as run, \
                patch.object(bridge, 'sim_refresh', return_value=False), \
                patch.object(job.time, 'sleep'), patch.object(job, 'state', state):
            result = job.operation(1, {'action': 'switch', 'iccid': '123', 'enabled': True})
        self.assertEqual(result, stale)
        self.assertEqual([call.args[1] for call in run.call_args_list].count(['profile', 'list']), 6)
        self.assertTrue(state['profile_state_pending'])
        self.assertEqual(state['warning'], 'refresh')

    def test_switch_returns_last_read_failure(self):
        error = {'ok': False, 'data': None, 'error': 'es10a_get_euicc_info', 'detail': ''}
        state = {}
        with patch.object(bridge, 'run_lpac', side_effect=[{'ok': True}] + [error] * 6) as run, \
                patch.object(bridge, 'sim_refresh', return_value=True), \
                patch.object(job.time, 'sleep'), patch.object(job, 'state', state):
            result = job.operation(1, {'action': 'switch', 'iccid': '123', 'enabled': False})
        self.assertEqual(result, error)
        self.assertEqual([call.args[1] for call in run.call_args_list].count(['profile', 'list']), 6)
        self.assertNotIn('profile_state_pending', state)

    def test_auto_report_stops_after_cancel_and_marks_pending(self):
        state = {}
        cancelled = [False]
        commands = []
        def notify(slot, seq, remove, aid, cancel=None):
            commands.append(['notification', 'process', '-r', str(seq)])
            cancelled[0] = True
            return {'ok': True, 'data': None, 'error': None, 'detail': None}
        pending = [{'seqNumber': 1}, {'seqNumber': 2}, {'seqNumber': 3}]
        with patch.object(job, 'state', state), patch.object(job, 'publish'), \
                patch.object(bridge, 'run_lpac', return_value={'ok': True, 'data': pending}), \
                patch.object(job, 'notify', side_effect=notify):
            job.report_new(1, [], cancel=lambda: cancelled[0])
        self.assertEqual(commands, [['notification', 'process', '-r', '1']])
        self.assertEqual(state['pending'], 2)
        self.assertEqual(state['warning'], 'notification_report')

    def test_delete_reports_only_new_notifications_in_order(self):
        requests = []
        before = [{'seqNumber': 10}]
        after = before + [{'seqNumber': 12}, {'seqNumber': 11}]
        replies = iter([{'ok': True, 'data': before}, {'ok': True}, {'ok': True, 'data': after}, bridge.failed(), {'ok': True}])
        def run(slot, argv, aid='', **kwargs):
            requests.append(argv)
            return next(replies)
        with patch.object(bridge, 'run_lpac', side_effect=run), patch.object(job, 'publish'), patch.object(job, 'state', {}):
            result = job.operation(1, {'action': 'delete', 'iccid': '123'})
            self.assertTrue(result['ok'])
            self.assertEqual(job.state['warning'], 'notification_report')
            self.assertEqual(job.state['failed'], 1)
        self.assertEqual(requests, [['notification', 'list'], ['profile', 'delete', '123'], ['notification', 'list'],
                                    ['notification', 'process', '-r', '11'], ['notification', 'process', '-r', '12']])

    def test_delete_reconciles_ambiguous_result_without_repeating_delete(self):
        error = {'ok': False, 'data': None, 'error': 'es10c_delete_profile',
                 'detail': 'internal error, maybe illegal iccid/aid coding'}
        cases = [
            ('removed', {'ok': True, 'data': []}, True),
            ('still_present', {'ok': True, 'data': [{'iccid': '123'}]}, False),
            ('unreadable', bridge.failed(), False),
        ]
        for label, profiles_result, expected_ok in cases:
            with self.subTest(label=label):
                replies = [{'ok': True, 'data': []}, error, profiles_result]
                if expected_ok:
                    replies += [{'ok': True, 'data': [{'seqNumber': 151}]}, {'ok': True}]
                with patch.object(bridge, 'run_lpac', side_effect=replies) as run, patch.object(job, 'publish'), patch.object(job, 'state', {}):
                    result = job.operation(1, {'action': 'delete', 'iccid': '123'})
                    self.assertEqual(result['ok'], expected_ok)
                    commands = [call.args[1] for call in run.call_args_list]
                    self.assertEqual(commands.count(['profile', 'delete', '123']), 1)
                    self.assertEqual(commands.count(['profile', 'list']), 1)
                    if expected_ok:
                        self.assertEqual(job.state['diagnostic'], error)
                        self.assertEqual(commands[-1], ['notification', 'process', '-r', '151'])
                    else:
                        self.assertEqual(result, error)

    def test_delete_policy_error_is_not_reclassified(self):
        error = {'ok': False, 'data': None, 'error': 'es10c_delete_profile', 'detail': 'disallowed by policy'}
        with patch.object(bridge, 'run_lpac', side_effect=[{'ok': True, 'data': []}, error]) as run:
            self.assertEqual(job.operation(1, {'action': 'delete', 'iccid': '123'}), error)
            self.assertEqual(run.call_count, 2)

    def test_background_cancellation_publishes_terminal_state_before_lock_release(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            worker = work / 'worker.py'
            worker.write_text(
                "import importlib.util,sys,types\n"
                "bridge=types.ModuleType('bridge')\n"
                "bridge.run_lpac=lambda *args, **kwargs: {'ok': False, 'data': None, 'error': 'cancelled', 'detail': ''}\n"
                "bridge.cancelled_result=lambda: {'ok': False, 'data': None, 'error': 'cancelled', 'detail': ''}\n"
                "sys.modules['bridge']=bridge\n"
                "spec=importlib.util.spec_from_file_location('job', %r)\n" % str(BASE / 'root/usr/lib/e5-esim/job.py') +
                "job=importlib.util.module_from_spec(spec); spec.loader.exec_module(job)\n"
                "job.STATE_DIR=%r\n" % directory +
                "job.STATUS=%r; job.PIDFILE=%r; job.FIFO=%r; job.CANCEL=%r\n" % tuple(str(work / name) for name in ('job.json', 'job.pid', 'job.in', 'cancel')) +
                "job.main(sys.argv[1:])\n")
            processes = []
            popen = rpc.subprocess.Popen
            def launch(*args, **kwargs):
                process = popen(*args, **kwargs)
                processes.append(process)
                return process
            with patch.multiple(rpc, STATE_DIR=directory, STATUS=str(work / 'job.json'),
                                PIDFILE=str(work / 'job.pid'), FIFO=str(work / 'job.in'),
                                LOCK=str(work / 'lock'), CANCEL=str(work / 'cancel'),
                                JOB=str(worker)), patch.object(rpc.subprocess, 'Popen', side_effect=launch):
                self.assertTrue(rpc.download_start({'slot': 1, 'activation': 'LPA:1$x',
                                                    'confirmation': '', 'aid': ''})['ok'])
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    status = rpc.job_status({})
                    if not status['running']:
                        break
                    time.sleep(.02)
            self.assertEqual(status['state'], 'cancelled')
            self.assertEqual(status['error'], 'cancelled')
            fd = rpc.acquire()
            os.close(fd)
            processes[0].wait(timeout=2)

    def test_background_operation_publishes_completion_and_releases_lock(self):
        import time
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            (work / 'bridge.py').write_text("def run_lpac(slot, argv, aid='', **kwargs):\n    return {'ok': True, 'data': [], 'error': None, 'detail': None}\n")
            worker = work / 'worker.py'
            worker.write_text(
                "import importlib.util,sys\n"
                "spec=importlib.util.spec_from_file_location('job', %r)\n" % str(BASE / 'root/usr/lib/e5-esim/job.py') +
                "job=importlib.util.module_from_spec(spec); spec.loader.exec_module(job)\n"
                "job.STATE_DIR=%r\n" % directory +
                "job.STATUS=%r; job.PIDFILE=%r; job.FIFO=%r; job.CANCEL=%r\n" % tuple(str(work / name) for name in ('job.json', 'job.pid', 'job.in', 'cancel')) +
                "job.main(sys.argv[1:])\n")
            processes = []
            popen = rpc.subprocess.Popen
            def launch(*args, **kwargs):
                process = popen(*args, **kwargs)
                processes.append(process)
                return process
            with patch.multiple(rpc, STATE_DIR=directory, STATUS=str(work / 'job.json'),
                                PIDFILE=str(work / 'job.pid'), FIFO=str(work / 'job.in'),
                                LOCK=str(work / 'lock'), CANCEL=str(work / 'cancel'),
                                JOB=str(worker)), patch.object(rpc.subprocess, 'Popen', side_effect=launch):
                result = rpc.operation_start({'slot': 1, 'aid': rpc.SE_AIDS[1], 'request': json.dumps({'action': 'rename', 'iccid': '123', 'name': 'test'})})
                self.assertTrue(result['ok'])
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    status = rpc.job_status({})
                    if not status['running']:
                        break
                    time.sleep(.02)
                self.assertEqual(status['state'], 'done')
                self.assertEqual(status['slot'], 1)
                self.assertEqual(status['aid'], rpc.SE_AIDS[1])
                self.assertEqual(status['action'], 'rename')
                processes[0].wait(timeout=2)
                fd = rpc.acquire()
                os.close(fd)

    def test_background_flows_carry_aid(self):
        aid = SE1
        delete_error = {'ok': False, 'data': None, 'error': 'es10c_delete_profile',
                        'detail': 'internal error, maybe illegal iccid/aid coding'}
        cases = [
            ('delete_reconcile', job.operation, (1, {'action': 'delete', 'iccid': '123'}),
             [{'ok': True, 'data': []}, delete_error, {'ok': True, 'data': []},
              {'ok': True, 'data': [{'seqNumber': 5}]}, {'ok': True}],
             [['notification', 'list'], ['profile', 'delete', '123'], ['profile', 'list'],
              ['notification', 'list'], ['notification', 'process', '-r', '5']]),
            ('notifications_batch', job.operation,
             (1, {'action': 'notifications', 'remove': False,
                  'notifications': [{'seqNumber': 9}, {'seqNumber': 3}]}),
             [{'ok': True}, {'ok': True}, {'ok': True, 'data': []}],
             [['notification', 'process', '-r', '3'], ['notification', 'process', '-r', '9'],
              ['notification', 'list']]),
            ('download', job.download, (1, 'LPA:1$x', ''),
             [{'ok': True, 'data': []}, {'ok': True},
              {'ok': True, 'data': [{'seqNumber': 7}]}, {'ok': True}],
             [['notification', 'list'], ['profile', 'download', '-p', '-a', 'LPA:1$x'],
              ['notification', 'list'], ['notification', 'process', '-r', '7']]),
        ]
        for label, function, args, replies, commands in cases:
            with self.subTest(label=label):
                calls = []
                replies = iter(replies)
                def run(slot, argv, aid='', **kwargs):
                    calls.append((slot, argv, aid))
                    return next(replies)
                with patch.object(bridge, 'run_lpac', side_effect=run), \
                        patch.object(job, 'publish'), patch.object(job, 'state', {}):
                    result = function(*args, aid)
                self.assertTrue(result['ok'])
                self.assertEqual([call[1] for call in calls], commands)
                self.assertTrue(all(call[0] == 1 and call[2] == aid for call in calls))

    def test_spawn_persists_slot_and_aid(self):
        recorded = []
        class Proc:
            pid = 4242
            def __init__(self, *args, **kwargs):
                self.stdin = io.BytesIO()
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with patch.multiple(rpc, STATE_DIR=directory, STATUS=str(work / 'job.json'),
                                PIDFILE=str(work / 'job.pid'), FIFO=str(work / 'job.in'),
                                LOCK=str(work / 'lock'), CANCEL=str(work / 'cancel'),
                                write_status=recorded.append), \
                    patch.object(rpc.subprocess, 'Popen', side_effect=Proc) as popen:
                self.assertTrue(rpc.download_start({'slot': 1, 'activation': 'LPA:1$x',
                                                    'confirmation': 'code', 'aid': SE0})['ok'])
                self.assertEqual(popen.call_args.args[0][2:],
                                 ['download', '1', 'LPA:1$x', 'code', SE0])
                request = {'action': 'rename', 'iccid': '123', 'name': 'n'}
                self.assertTrue(rpc.operation_start({'slot': 0, 'request': json.dumps(request),
                                                     'aid': SE1})['ok'])
                self.assertEqual(popen.call_args.args[0][2:],
                                 ['operation', '0', json.dumps(request), SE1])
        self.assertEqual([(item['type'], item['slot'], item['aid']) for item in recorded],
                         [('download', 1, SE0), ('operation', 0, SE1)])
        self.assertTrue(all(item['state'] == 'running' for item in recorded))

    def test_run_lpac_clears_inherited_aid_for_standard_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'aid'
            script = Path(directory) / 'lpac'
            script.write_text('#!/bin/sh\nprintf "%%s" "${LPAC_CUSTOM_ISD_R_AID-unset}" > %r\n' % str(output))
            script.chmod(0o755)
            with patch.object(bridge, 'LPAC', str(script)), \
                    patch.object(bridge, 'open_device', return_value=None), \
                    patch.dict(os.environ, {'LPAC_CUSTOM_ISD_R_AID': 'stale'}):
                bridge.run_lpac(0, ['chip', 'info'])
                self.assertEqual(output.read_text(), 'unset')
                bridge.run_lpac(0, ['chip', 'info'], aid=SE0)
                self.assertEqual(output.read_text(), SE0)

    def test_dependency_status_uses_bridge_artifact_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            lpac = work / 'lpac'
            lpac.write_text('#!/bin/sh\n')
            lpac.chmod(0o755)
            library = work / 'libcurl-gnutls.so.4'
            library.write_bytes(b'')
            with patch.object(rpc.bridge, 'LPAC', str(lpac)), \
                    patch.object(rpc.bridge, 'GNUTLS_CURL', str(library)):
                result = rpc.dependencies({})
        self.assertTrue(result['ok'])
        self.assertEqual(result['data']['ready'], True)
        self.assertEqual(result['data']['missing'], [])
        self.assertEqual([(item['package'], item['path'], item['present']) for item in result['data']['items']],
                         [('lpac', str(lpac), True), ('libcurl-gnutls4', str(library), True)])

    def test_dependency_status_reports_missing_or_unusable_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            lpac = work / 'lpac'
            lpac.write_text('')
            with patch.object(rpc.bridge, 'LPAC', str(lpac)), \
                    patch.object(rpc.bridge, 'GNUTLS_CURL', str(work / 'missing.so')):
                result = rpc.dependencies({})
        self.assertFalse(result['data']['ready'])
        self.assertEqual(result['data']['missing'], ['lpac', 'libcurl-gnutls4'])
        self.assertEqual([item['present'] for item in result['data']['items']], [False, False])

    def test_integer_parameters_are_not_truncated(self):
        for value in (True, .5, '1', -1, 2):
            with self.assertRaises(rpc.Invalid):
                rpc.slot_of({'slot': value})
        for value in (True, 1.5, -1, 2**32):
            with self.assertRaises(rpc.Invalid):
                rpc.seq_of({'seq': value})


if __name__ == '__main__':
    unittest.main()
