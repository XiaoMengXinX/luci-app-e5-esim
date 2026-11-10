import json
import os
import pty
import re
import select
import signal
import subprocess
import time
import tty

LPAC = '/usr/lib/lpac'
GNUTLS_CURL = '/usr/lib/libcurl-gnutls.so.4'
TLS_SHIM = '/tmp/e5-lpac-tls'
AT_DEV = os.environ.get('E5_ESIM_AT_DEV') or '/dev/stty_nr4'
ESTK_AID = 'A06573746B6D65FFFFFFFFFFFF6D6774'

# The CP powers the standby SIM down after APDU traffic; these are the CME codes
# the firmware answers once it has.
GONE = {'10', '21', '100', '3'}
CHANNEL_TIMEOUT = 20
TRANSMIT_TIMEOUT = 25
SIM_TIMEOUT = 10
PROBES = 5
PERIOD = 0.04
CANCEL_GRACE = 3
HEX = '0123456789abcdefABCDEF'


def failed():
    return {'ok': False, 'data': None, 'error': 'lpac', 'detail': ''}


def cancelled_result():
    return {'ok': False, 'data': None, 'error': 'cancelled', 'detail': ''}


def tls_shim(env):
    """The mbedTLS build of libcurl rejects certificate chains with critical
    extensions it does not parse (the GSMA RSP2 roots' Certificate Policies),
    which SM-DP+ and notification servers serve; the GnuTLS build accepts them."""
    if not os.access(GNUTLS_CURL, os.R_OK):
        return
    os.makedirs(TLS_SHIM, mode=0o700, exist_ok=True)
    link = os.path.join(TLS_SHIM, 'libcurl.so.4')
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass
    try:
        os.symlink(GNUTLS_CURL, link)
    except OSError:
        return
    env['LD_LIBRARY_PATH'] = TLS_SHIM


def open_device():
    try:
        device = os.open(AT_DEV, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        tty.setraw(device)
    except OSError:
        pass
    return device


def has_line(reply, text):
    return text in reply.replace('\r', '\n').split('\n')


def complete(reply):
    return '+CME ERROR' in reply or has_line(reply, 'OK') or has_line(reply, 'ERROR')


def cme_error(reply):
    index = reply.find('+CME ERROR:')
    if index < 0:
        return None
    digits = ''
    for char in reply[index + 11:].lstrip(' '):
        if not char.isdigit():
            break
        digits += char
    return digits or None


def channel_of(reply):
    index = reply.find('+CCHO:')
    if index < 0:
        return None
    digits = ''
    for char in reply[index + 6:].lstrip(' '):
        if not char.isdigit():
            break
        digits += char
    return digits or None


def cgla_line(reply):
    index = reply.find('+CGLA:')
    if index < 0:
        return None
    return re.split(r'[\r\n]', reply[index:])[0] or None


def apdu_of(body):
    fields = body.split(',', 2)
    if len(fields) != 3:
        return None
    open_at = fields[2].find('"')
    if open_at < 0:
        return None
    close_at = fields[2].find('"', open_at + 1)
    if close_at < 0:
        return None
    apdu = fields[2][open_at + 1:close_at]
    if not apdu or any(char not in HEX for char in apdu):
        return None
    return apdu.upper()


class At:
    def __init__(self, device, card, poll=lambda: None):
        self.device = device
        self.card = card
        self.poll = poll
        self.active = False

    def drain(self):
        while select.select([self.device], [], [], 0)[0]:
            try:
                if not os.read(self.device, 256):
                    return
            except OSError:
                return

    def command(self, body, timeout):
        if self.device is None:
            return ''
        self.poll()
        self.active = True
        try:
            self.drain()
            os.write(self.device, ('AT+SPACTCARD=%d;%s\r\n' % (self.card, body)).encode())
            reply = ''
            end = time.monotonic() + timeout
            while not complete(reply):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self.poll()
                if not select.select([self.device], [], [], min(remaining, PERIOD))[0]:
                    continue
                try:
                    chunk = os.read(self.device, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                reply += chunk.decode('utf-8', 'replace')
            return reply
        finally:
            self.active = False


class WriteProgress:
    def __init__(self, notify):
        self.notify = notify
        self.active = False
        self.total = 0
        self.written = 0
        self.percent = -1

    def accept(self, apdu, reply):
        if not self.active:
            return
        raw = bytes.fromhex(apdu)
        if len(raw) < 5 or raw[1] != 0xe2:
            return
        response = re.search(r',\s*"?([0-9a-fA-F]+)', reply)
        if not response:
            return
        status = response.group(1)[-4:].upper()
        if not status.startswith(('61', '9')):
            return
        payload = raw[5:5 + raw[4]]
        if not self.total:
            # BF36's BER length covers the complete bound profile package.
            if not payload.startswith(b'\xbf\x36') or len(payload) < 3:
                return
            length = payload[2]
            header = 3
            if length & 0x80:
                count = length & 0x7f
                if not 1 <= count <= 4 or len(payload) < 3 + count:
                    return
                header += count
                length = int.from_bytes(payload[3:header], 'big')
            self.total = header + length
        self.written += len(payload)
        percent = min(100, self.written * 100 // self.total)
        if percent != self.percent:
            self.percent = percent
            self.notify('es10b_load_bound_profile_package:progress',
                        {'written': self.written, 'total': self.total, 'percent': percent})


class Bridge:
    """Answers lpac's AT lines.  The channel number lpac sends back is only a
    label; the one the card handed out is kept here, and it changes whenever
    the card is recovered."""

    def __init__(self, master, ring, on_transmit=None):
        self.master = master
        self.ring = ring
        self.on_transmit = on_transmit
        self.aid = ''
        self.channel = None
        self.open = False
        self.retried = False
        self.transmit_error = ''

    def say(self, line):
        os.write(self.master, (line + '\r\n').encode())

    def fail(self, code):
        self.say('+CME ERROR: %s' % code)
        self.say('ERROR')

    def accept(self, reply):
        if cme_error(reply):
            return None
        channel = channel_of(reply)
        if not channel or channel == '0':
            return None
        self.channel = channel
        self.open = True
        return channel

    def sweep(self):
        """Leaked logical channels make CCHO fail with CME 22; the eUICC has 1
        to 3, so closing all of them clears the table."""
        for number in (1, 2, 3):
            self.ring.command('+CSIM=10,"0070800%d00"' % number, SIM_TIMEOUT)

    def recover(self):
        # +SFUN=2 alone revives the card; anything appended to it powers it down
        # again.
        self.ring.command('+SFUN=2', CHANNEL_TIMEOUT)
        for _ in range(PROBES):
            time.sleep(1)
            if '+CCID:' in self.ring.command('+CCID', SIM_TIMEOUT):
                return

    def open_channel(self):
        self.open = False
        reply = self.ring.command('+CCHO="%s"' % self.aid, CHANNEL_TIMEOUT)
        channel = self.accept(reply)
        if channel:
            return channel
        code = cme_error(reply) or '10'
        self.sweep()
        if code in GONE:
            self.recover()
        return self.accept(self.ring.command('+CCHO="%s"' % self.aid, CHANNEL_TIMEOUT))

    def transmit(self, apdu):
        command = '+CGLA=%s,%d,"%s"' % (self.channel, len(apdu), apdu)
        reply = self.ring.command(command, TRANSMIT_TIMEOUT)
        code = cme_error(reply)
        if code is None:
            line = cgla_line(reply)
            if not line:
                self.say('ERROR')
                return
            if self.on_transmit:
                self.on_transmit(apdu, line)
            self.say(line)
            self.say('')
            self.say('OK')
            return
        if code in GONE and self.aid and not self.retried:
            self.retried = True
            self.transmit_error = code
            self.recover()
            if self.open_channel() is None:
                self.fail(self.transmit_error)
                return
            self.transmit(apdu)
            return
        self.fail(code)

    def request(self, body):
        apdu = apdu_of(body)
        if apdu is None:
            self.say('ERROR')
            return
        if not self.open:
            self.fail('21')
            return
        self.retried = False
        self.transmit(apdu)

    def line(self, text):
        if not text or text[:2].upper() != 'AT':
            return
        body = text[2:]
        upper = body.upper()
        if not body:
            self.say('OK')
        elif body.endswith('=?'):
            self.say('OK')
        elif upper.startswith('+CCHO="'):
            self.aid = body[7:].split('"')[0][:63]
            channel = self.open_channel()
            if channel is None:
                self.fail('10')
                return
            self.say('+CCHO: %s' % channel)
            self.say('')
            self.say('OK')
        elif upper.startswith('+CGLA='):
            self.request(body[6:])
        elif upper.startswith('+CCHC='):
            if self.open:
                self.ring.command('+CCHC=%s' % self.channel, CHANNEL_TIMEOUT)
                self.open = False
                self.channel = None
            self.say('OK')
        else:
            reply = self.ring.command(body, CHANNEL_TIMEOUT)
            self.say('OK' if has_line(reply, 'OK') else 'ERROR')


def sim_refresh(card):
    """The modem only sees a profile switch after its SIM interface restarts:
    radio off, radio on, then SIM power-up."""
    device = open_device()
    if device is None:
        return False
    try:
        ring = At(device, card)
        replies = []
        for index, body in enumerate(('+SFUN=5', '+SFUN=4', '+SFUN=2')):
            if index:
                time.sleep(1)
            replies.append(ring.command(body, CHANNEL_TIMEOUT))
        return all(has_line(reply, 'OK') for reply in replies)
    finally:
        os.close(device)


class Deadline(Exception):
    pass


def probe_estk(card):
    """Probes the eSTK vendor applet; must not switch or reset the SIM."""
    device = open_device()
    if device is None:
        return False
    try:
        ring = At(device, card)
        reply = ring.command('+CCHO="%s"' % ESTK_AID, CHANNEL_TIMEOUT)
        if not has_line(reply, 'OK'):
            return False
        channel = channel_of(reply)
        if not channel or channel == '0':
            return False
        return has_line(ring.command('+CCHC=%s' % channel, CHANNEL_TIMEOUT), 'OK')
    finally:
        os.close(device)


def run_lpac(card, argv, on_progress=None, on_preview=None, cancel=None, aid=''):
    argv = list(argv)
    watchdog = 300 if argv[:2] == ['profile', 'download'] else 120
    env = dict(os.environ)
    env['LPAC_APDU'] = 'at'
    env['LPAC_HTTP'] = 'curl'
    # Do not let a selected SE leak into the standard ISD-R through the caller's
    # environment.
    if aid:
        env['LPAC_CUSTOM_ISD_R_AID'] = aid
    else:
        env.pop('LPAC_CUSTOM_ISD_R_AID', None)
    tls_shim(env)
    master = slave = None
    try:
        master, slave = pty.openpty()
        tty.setraw(slave)
        env['LPAC_APDU_AT_DEVICE'] = os.ttyname(slave)
        proc = subprocess.Popen([LPAC] + argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, env=env)
    except OSError:
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        return failed()
    device = open_device()
    outcome = {'result': None, 'started': time.monotonic(), 'cancel_at': None,
               'deadline_at': None, 'active': False, 'cleanup_used': False}
    writer = WriteProgress(on_progress) if on_progress and argv[:2] == ['profile', 'download'] else None

    def poll():
        now = time.monotonic()
        was_active = outcome['active']
        outcome['active'] = active = ring.active
        if outcome['cancel_at'] is None and cancel is not None and cancel():
            outcome['cancel_at'] = now
        if outcome['cancel_at'] is not None:
            if active:
                return
            if outcome['deadline_at'] is None:
                outcome['deadline_at'] = now + CANCEL_GRACE
                if proc.poll() is None:
                    proc.send_signal(signal.SIGINT)
                return
            if was_active and now >= outcome['deadline_at'] and not outcome['cleanup_used']:
                outcome['cleanup_used'] = True
                outcome['deadline_at'] = now + CANCEL_GRACE
            if now >= outcome['deadline_at']:
                raise Deadline()
        elif not active and now - outcome['started'] >= watchdog:
            raise Deadline()

    def send(text):
        try:
            proc.stdin.write(text.encode())
            proc.stdin.flush()
        except OSError:
            pass

    def take(text):
        try:
            doc = json.loads(text)
        except ValueError:
            return
        payload = doc.get('payload') or {}
        kind = doc.get('type')
        if kind == 'progress':
            message = payload.get('message') or ''
            if writer:
                writer.active = message == 'es10b_load_bound_profile_package'
            if message == 'preview':
                accept = on_preview is not None and outcome['cancel_at'] is None and on_preview()
                outcome['started'] = time.monotonic()
                send('y\n' if accept else 'n\n')
            elif on_progress:
                on_progress(message, payload.get('data'))
            return
        if kind != 'lpa':
            return
        data = payload.get('data')
        ok = payload.get('code') == 0
        outcome['result'] = {'ok': ok,
                             'data': data if ok else None,
                             'error': None if ok else (payload.get('message') or ''),
                             'detail': None if ok else (data if isinstance(data, str) else '')}

    ring = At(device, card, poll)
    bridge = Bridge(master, ring, writer.accept if writer else None)
    out = b''
    pending = b''
    out_fd = proc.stdout.fileno()
    try:
        while True:
            poll()
            ready = select.select([out_fd, master], [], [], PERIOD)[0]
            if out_fd in ready:
                chunk = os.read(out_fd, 65536)
                if not chunk:
                    if out:
                        take(out.decode('utf-8', 'replace'))
                    break
                out += chunk
                lines = out.split(b'\n')
                out = lines.pop()
                for line in lines:
                    take(line.decode('utf-8', 'replace'))
            if master in ready:
                pending += os.read(master, 4096)
                lines = re.split(rb'[\r\n]', pending)
                pending = lines.pop()
                for line in lines:
                    bridge.line(line.decode('utf-8', 'replace'))
        while proc.poll() is None:
            poll()
            time.sleep(PERIOD)
    except Deadline:
        pass
    except OSError:
        if outcome['cancel_at'] is None:
            outcome['result'] = failed()
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()
        for fd in (master, slave, device):
            if fd is not None:
                os.close(fd)
    if outcome['result'] is not None and outcome['result']['ok']:
        return outcome['result']
    if outcome['cancel_at'] is not None:
        return cancelled_result()
    return outcome['result'] or failed()
