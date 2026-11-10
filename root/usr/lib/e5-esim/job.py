import json
import os
import select
import signal
import sys
import time

import bridge

STATE_DIR = '/tmp/e5-esim'
STATUS = os.path.join(STATE_DIR, 'job.json')
PIDFILE = os.path.join(STATE_DIR, 'job.pid')
FIFO = os.path.join(STATE_DIR, 'job.in')
CANCEL = os.path.join(STATE_DIR, 'cancel')
LOCK_FD = 'E5_ESIM_LOCK_FD'

state = {'type': None, 'state': 'running', 'step': None, 'preview': False, 'metadata': None,
         'error': None, 'detail': None}

signal_cancelled = False


def publish():
    path = STATUS + '.tmp'
    with open(path, 'w') as file:
        json.dump(state, file)
    os.replace(path, STATUS)


def stop(number, frame):
    global signal_cancelled
    signal_cancelled = True


def cancel_requested():
    if signal_cancelled:
        return True
    try:
        with open(CANCEL) as file:
            return int(file.read().strip()) == os.getpid()
    except (OSError, ValueError):
        return False


def remove_cancel():
    try:
        with open(CANCEL) as file:
            if int(file.read().strip()) != os.getpid():
                return
    except (OSError, ValueError):
        return
    try:
        os.unlink(CANCEL)
    except FileNotFoundError:
        pass


def release_lock():
    try:
        fd = int(os.environ.get(LOCK_FD, ''))
    except (TypeError, ValueError):
        return
    try:
        os.close(fd)
    except OSError:
        pass


def remove_pidfile():
    try:
        with open(PIDFILE) as file:
            if int(file.read().strip()) != os.getpid():
                return
    except (OSError, ValueError):
        return
    try:
        os.unlink(PIDFILE)
    except FileNotFoundError:
        pass


def preview():
    # O_RDWR: a reader-only FIFO reports EOF (and spins) until a writer shows up.
    fd = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)
    answer = False
    try:
        state['preview'] = True
        publish()
        while not cancel_requested():
            if not select.select([fd], [], [], 0.5)[0]:
                continue
            data = os.read(fd, 16)
            if data[:1] in (b'y', b'n'):
                answer = data[:1] == b'y'
                break
    finally:
        state['preview'] = False
        publish()
        os.close(fd)
    return answer


def progress(message, data):
    if message == 'es10b_load_bound_profile_package:progress':
        state['write'] = data
        publish()
        return
    # The apk lpac 2.3.0 prints es8p_meatadata_parse (misspelled); newer builds
    # print es8p_metadata_parse.
    if message in ('es8p_metadata_parse', 'es8p_meatadata_parse'):
        state['metadata'] = data
    state['step'] = message
    publish()


def notify(slot, seq, remove, aid, cancel=None):
    argv = ['notification', 'remove', str(seq)] if remove else ['notification', 'process', '-r', str(seq)]
    if cancel is None:
        return bridge.run_lpac(slot, argv, aid=aid)
    return bridge.run_lpac(slot, argv, aid=aid, cancel=cancel)


def handle_notifications(slot, notifications, remove=False, aid='', cancel=None):
    processed = failed = pending = 0
    items = sorted(item['seqNumber'] for item in notifications)
    for index, seq in enumerate(items):
        if cancel is not None and cancel():
            pending = len(items) - index
            break
        state.update(step='notifications', current=index + 1, total=len(items))
        publish()
        if notify(slot, seq, remove, aid, cancel=cancel)['ok']:
            processed += 1
        else:
            failed += 1
    state.update(processed=processed, failed=failed)
    if pending:
        state['pending'] = pending


def report_new(slot, before, aid='', cancel=None):
    if cancel is None:
        result = bridge.run_lpac(slot, ['notification', 'list'], aid=aid)
    else:
        result = bridge.run_lpac(slot, ['notification', 'list'], aid=aid, cancel=cancel)
    if not result['ok']:
        state['warning'] = 'notification_list'
        return
    seen = {item['seqNumber'] for item in before}
    handle_notifications(slot, [item for item in result['data'] if item['seqNumber'] not in seen],
                         aid=aid, cancel=cancel)
    if state.get('failed') or state.get('pending'):
        state['warning'] = 'notification_report'


def operation(slot, request, aid=''):
    action = request['action']
    if action == 'switch':
        result = bridge.run_lpac(slot, ['profile', 'enable' if request['enabled'] else 'disable', request['iccid']], aid=aid)
        if not result['ok']:
            return result
        if not bridge.sim_refresh(slot):
            state['warning'] = 'refresh'
        expected = 'enabled' if request['enabled'] else 'disabled'
        # The SIM answers stale state while it initializes.
        for _ in range(6):
            time.sleep(3)
            result = bridge.run_lpac(slot, ['profile', 'list'], aid=aid)
            if result['ok'] and any(profile.get('iccid') == request['iccid'] and
                                    profile.get('profileState') == expected for profile in result['data'] or []):
                return result
        if result['ok']:
            state['profile_state_pending'] = True
        return result
    if action == 'rename':
        result = bridge.run_lpac(slot, ['profile', 'nickname', request['iccid'], request['name']], aid=aid)
        return bridge.run_lpac(slot, ['profile', 'list'], aid=aid) if result['ok'] else result
    if action == 'delete':
        before = bridge.run_lpac(slot, ['notification', 'list'], aid=aid)
        if not before['ok']:
            return before
        result = bridge.run_lpac(slot, ['profile', 'delete', request['iccid']], aid=aid)
        if (not result['ok'] and result['error'] == 'es10c_delete_profile' and
                result['detail'] == 'internal error, maybe illegal iccid/aid coding'):
            # The card may commit deletion before lpac loses the response.
            profiles = bridge.run_lpac(slot, ['profile', 'list'], aid=aid)
            if profiles['ok'] and not any(profile['iccid'] == request['iccid'] for profile in profiles['data']):
                state['diagnostic'] = result
                result = {'ok': True, 'data': None, 'error': None, 'detail': None}
        if result['ok']:
            report_new(slot, before['data'], aid=aid)
        return result
    if request.get('single'):
        item = request['notifications'][0]
        result = notify(slot, item['seqNumber'], request['remove'], aid)
        if not result['ok']:
            return result
        state.update(processed=1, failed=0)
    else:
        handle_notifications(slot, request['notifications'], request['remove'], aid=aid)
    return bridge.run_lpac(slot, ['notification', 'list'], aid=aid)


def download(slot, activation, confirmation, aid=''):
    argv = ['profile', 'download', '-p', '-a', activation]
    if confirmation:
        argv += ['-c', confirmation]
    before = bridge.run_lpac(slot, ['notification', 'list'], aid=aid, cancel=cancel_requested)
    if cancel_requested():
        return bridge.cancelled_result()
    if not before['ok']:
        return before
    result = bridge.run_lpac(slot, argv, on_progress=progress, on_preview=preview,
                             cancel=cancel_requested, aid=aid)
    if result['ok']:
        state['step'] = 'reporting'
        publish()
        report_new(slot, before['data'], aid=aid, cancel=cancel_requested)
    return result


def main(argv):
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    state['type'] = argv[0]
    state['slot'] = int(argv[1])
    state['aid'] = argv[4 if argv[0] == 'download' else 3]
    if sys.stdin.read(1) != '1':
        return 1
    try:
        if argv[0] == 'download':
            result = download(state['slot'], argv[2], argv[3], state['aid'])
        else:
            request = json.loads(argv[2])
            state['action'] = request['action']
            state['remove'] = request.get('remove', False)
            publish()
            result = operation(state['slot'], request, state['aid'])
    except Exception as error:
        result = {'ok': False, 'error': 'lpac', 'detail': str(error)}
    state['data'] = result.get('data')
    state['state'] = 'done' if result['ok'] else (
        'cancelled' if result['error'] == 'cancelled' else 'error')
    state['preview'] = False
    state['error'] = result['error']
    state['detail'] = result['detail']
    publish()
    remove_cancel()
    release_lock()
    remove_pidfile()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
