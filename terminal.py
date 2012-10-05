# -*- coding: utf-8 -*-
# utf-8 test 很高兴见到你
import multiprocessing
import logging
import threading
import time
import re
import blessings

# global list of (TelnetClient, multiprocessing.Pipe, threading.Lock)
# this is a shared global variable across threads.
registry = list ()
logger = logging.getLogger(__name__)
logger.setLevel (logging.DEBUG)

def start_process(child_conn, termtype, rows, columns, charset, origin):
  import bbs.session
  stream = IPCStream(child_conn)
  term = BlessedIPCTerminal (stream, termtype, rows, columns)
  new_session = bbs.session.Session (term, child_conn, charset, origin)
  # provide the curses-like 'restore screen to original on exit' trick
  enter, exit = term.enter_fullscreen(), term.exit_fullscreen()
  if 0 not in (len(enter), len(exit),):
    with term.fullscreen():
      new_session.run ()
  else:
    # terminal is not capable, ignore!
    new_session.run ()
  logger.debug ('%s/%s end process', new_session.pid, new_session.handle)
  new_session.close ()
  child_conn.send (('disconnect', 'process exit',))


class IPCStream(object):
  """
  connect blessings 'stream' to 'child' multiprocessing.Pipe
  only write(), fileno(), and close() are called by blessings.
  """
  def __init__(self, channel):
    self.channel = channel
  def write(self, data, cp437=False):
    assert issubclass(type(data), unicode)
    self.channel.send (('output', (data, cp437)))
  def fileno(self):
    return self.channel.fileno()
  def close(self):
    return self.channel.close ()


class BlessedIPCTerminal(blessings.Terminal):
  """
    Furthermore, .rows and .columns no longer queries using termios routines.
    They must be managed by another procedure.
    Instances of this class are stored in the global registry (list)
  """
  def __init__(self, stream, terminal_type, rows, columns):
    # patch in a .rows and .columns static property.
    # this property updated by engine.py poll routine (?)
    self.rows, self.columns = rows, columns
    try:
      blessings.Terminal.__init__ (self,
          kind=terminal_type, stream=stream, force_styling=True)
      self.kind = terminal_type
      logging.debug ('setupterm(%s) succesfull', terminal_type,)

    except blessings.curses.error, e:
      from bbs import ini
      # when setupterm() fails with client-supplied terminal_type
      # try again using the configuration .ini default type.
      default_ttype = ini.cfg.get('session', 'default_ttype')
      errmsg = 'setupterm(%s) failed: %s' % (terminal_type, e,)
      assert terminal_type != default_ttype, \
          '%s; using default_ttype' % (errmsg,)
      logger.warn (errmsg)
      stream.write (errmsg + '\r\n')
      stream.write ('terminal type: %s (default)\r\n' % (default_ttype,))
      blessings.Terminal.__init__ (self,
          kind=default_ttype, stream=stream, force_styling=True)
      self.kind = default_ttype

  def keyname(self, keycode):
    """Return any matching keycode name for a given keycode."""
    try:
      return (a for a in dir(self) if a.startswith('KEY_') and keycode ==
          getattr(self, a)).next ()
    except StopIteration:
      logger.warn ('keycode unmatched %r', keycode)

  @property
  def terminal_type(self):
    return self.kind

  @terminal_type.setter
  def terminal_type(self, value):
    if value != self.kind:
      self.kind = value
      logger.warn ('TERM=%s; cannot re-initialize curses' % (value,))

  def _height_and_width(self):
    """Return a tuple of (terminal height, terminal width)."""
    return self.rows, self.columns

def on_disconnect(client):
  """Discover the matching client in registry and remove it"""
  import copy
  global registry
  logger.info ('Disconnected from telnet client %s:%s',
      client.address, client.port)
  for (c,p,l) in registry[:]:
    if client == c:
      registry.remove ((c,p,l))

def on_connect(client):
  """Spawn a ConnectTelnetTerminal() thread for each new connection."""
  logger.info ('Connection from telnet client %s:%s',
      client.address, client.port)
  t = ConnectTelnetTerminal(client)
  t.start ()

def on_naws(client):
  """On a NAWS event, check if client is yet registered in registry and
     send the pipe a refresh event. This is the same thing as ^L to the
     'userland', but should indicate also that the window sizes are checked."""
  for (c,p,l) in registry:
    if client == c:
      p.send (('refresh', ('resize', (c.columns, c.rows),)))
      return

class ConnectTelnetTerminal (threading.Thread):
  """
  This thread spawns long enough to
    1. set socket and telnet options
    2. ask about terminal type and size
    3. start a new session (as a sub-process)

  TODO: something useful with identd, like IRCd does
  """
  DEBUG = False
  TIME_WAIT = 1.25
  TIME_PAUSE = 1.75
  TIME_POLL  = 0.01
  TTYPE_UNDETECTED = 'unknown client'
  CHARSET_UNDETECTED = 'unknown encoding'
  WINSIZE_TRICK= (
        ('vt100', ('\x1b[6n'), re.compile('\033' + r"\[(\d+);(\d+)R")),
        ('sun', ('\x1b[18t'), re.compile('\033' + r"\[8;(\d+);(\d+)t"))
  ) # see: xresize.c from X11.org


  def __init__(self, client):
    self.client = client
    threading.Thread.__init__(self)


  def _spawn_session(self):
    """ Spawn a subprocess, avoiding GIL and forcing all shared data over a
        pipe. Previous versions of x/84 and prsv were single process,
        thread-based, and shared variables.

        All IPC communication occurs through the bi-directional pipe. The
        server end polls the parent end of a pipe in registry, while
        the client polls the child end as getsession().pipe.
    """
    global registry
    parent_conn, child_conn = multiprocessing.Pipe()
    lock = threading.Lock()
    p = multiprocessing.Process \
        (target=start_process,
           args=(child_conn, self.client.terminal_type,
            self.client.rows, self.client.columns,
            self.client.charset, self.client.addrport(),))
    p.start ()
    registry.append ((self.client, parent_conn, lock))

  def banner(self):
    #self.client.send_str (bytes('\xff\xfb\x01')) # will echo
    #self.client.send_str (bytes('\
    # disable line-wrapping http://www.termsys.demon.co.uk/vtansi.htm
    self.client.send_str (bytes('\033[7l'))

  def run(self):
    """Negotiate and inquire about terminal type, telnet options,
    window size, and tcp socket options before spawning a new session."""
    import socket
    from bbs import exception
    try:
      logger.debug ('_set_socket_opts')
      self._set_socket_opts ()
    except socket.error, e:
      logger.info ('Socket error during negotiation: %s', e)
      return

    try:
      self.banner ()
      logger.debug ('_try_echo')
      self._try_echo ()
      logger.debug ('_try_sga')
      self._try_sga ()
      # according to the internets, 'sga + echo' means
      # character mode. at least 'linux understands this'
      #logger.debug ('_no_linemode')
      #self._no_linemode ()
      logger.debug ('_try_naws')
      self._try_naws ()
      logger.debug ('_try_ttype')
      self._try_ttype ()
      logger.debug ('_try_charset')
      self._try_charset ()
      logger.debug ('_try_binary')
      self._try_binary ()
      logger.debug ('_spawn_session')
      self._spawn_session ()

    except exception.ConnectionClosed, e:
      logger.info ('Connection closed during negotiation: %s', e)

  def _timeleft(self, t):
    """Returns True when difference of current time and t is below TIME_WAIT"""
    return bool(time.time() -t < self.TIME_WAIT)


  def _set_socket_opts(self):
    """Set socket non-blocking and enable TCP KeepAlive"""
    import socket
    self.client.sock.setblocking (0)
    self.client.sock.setsockopt (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)


  def _no_linemode (self):
    """Negotiate line mode (LINEMODE) telnet option (off)."""
    from telnet import LINEMODE
    self.client._iac_dont(LINEMODE)
    self.client._iac_wont(LINEMODE)
    self.client._note_reply_pending(LINEMODE, True)


  def _try_binary(self):
    """Negotiation binary (BINARY) telnet option (on)."""
    from telnet import BINARY
    if self.client.telnet_eight_bit:
      logger.info ('binary mode enabled (unsolicted)')
      return

    logger.debug('request-do-eight-bit')
    self.client.request_do_binary ()
    self.client.socket_send() # push
    t = time.time()
    while self.client.telnet_eight_bit is False and self._timeleft(t):
      time.sleep (self.TIME_POLL)
    if self.client.telnet_eight_bit:
      logger.info ('binary mode enabled (negotiated)')
    else:
      logger.debug ('failed: binary; ignoring')


  def _try_sga(self):
    """Negotiate supress go-ahead (SGA) telnet option (on)."""
    from telnet import SGA
    enabledRemote = self.client._check_remote_option
    enabledLocal = self.client._check_local_option
    if enabledRemote(SGA) is True and enabledLocal(SGA) is True:
      logger.info ('sga enabled')
      return

    logger.debug('request-do-sga')
    self.client.request_will_sga ()
    self.client.socket_send() # push
    t = time.time()
    while not (enabledRemote(SGA) is True and enabledLocal(SGA) is True) \
      and self._timeleft(t):
        time.sleep (self.TIME_POLL)
    if (enabledRemote(SGA) is True and enabledLocal(SGA) is True):
      logger.info ('sga enabled (negotiated)')
    else:
      logger.info ('failed: supress go-ahead')


  def _try_echo(self):
    """Negotiate echo (ECHO) telnet option (on)."""
    if self.client.telnet_echo is True:
      logger.debug ('echo enabled')
      return
    logger.debug('request-will-echo')
    self.client.request_will_echo ()
    self.client.socket_send() # push
    t = time.time()
    while self.client.telnet_echo is False \
        and self._timeleft(t):
      time.sleep (self.TIME_POLL)
    if self.client.telnet_echo:
      logger.debug ('echo enabled (negotiated)')
    else:
      logger.debug ('failed: echo, ignored !')


  def _try_naws(self):
    """Negotiate about window size (NAWS) telnet option (on)."""
    if not None in (self.client.columns, self.client.rows,):
      logger.debug ('window size: %dx%d (unsolicited)' \
          % (self.client.columns, self.client.rows,))
      return

    logger.debug('request-naws')
    self.client.request_do_naws ()
    self.client.socket_send() # push
    t = time.time()
    while None in (self.client.columns, self.client.rows,) \
      and self._timeleft(t):
        time.sleep (self.TIME_POLL)
    if not None in (self.client.columns, self.client.rows,):
      logger.info ('window size: %dx%d (negotiated)' \
          % (self.client.columns, self.client.rows,))
      return
    logger.debug ('failed: negotiate about window size')

    # Try #2 ... this works for most any screen
    # send to client --> pos(999,999)
    # send to client --> report cursor position
    # read from client <-- window size
    logger.debug ('store-cu')
    self.client.send_str ('\x1b[s')
    for kind, query_seq, response_pattern in self.WINSIZE_TRICK:
      logger.debug ('move-to corner & query for %s' % (kind,))
      self.client.send_str ('\x1b[999;999H')
      self.client.send_str (query_seq)
      self.client.socket_send() # push
      inp=''
      t = time.time()
      while self.client.idle() < self.TIME_PAUSE and self._timeleft(t):
        time.sleep (self.TIME_POLL)
      inp = self.client.get_input()
      self.client.send_str ('\x1b[r')
      logger.debug ('cursor restored')
      self.client.socket_send() # push
      match = response_pattern.search (inp)
      if match:
        h, w = match.groups()
        self.client.rows, self.client.columns = int(h), int(w)
        logger.info ('window size: %dx%d (corner-query hack)' \
            % (self.client.columns, self.client.rows,))
        return

    logger.debug ('failed: negotiate about window size')
    # set to 80x24 if not detected
    self.client.columns, self.client.rows = 80, 24
    logger.debug ('window size: %dx%d (default)' \
        % (self.client.columns, self.client.rows,))


  def _try_charset(self):
    """Negotiate terminal charset (CHARSET) telnet option (on)."""
    # haven't seen this work yet ...
    from bbs import ini
    from telnet import CHARSET
    if self.client.charset != self.CHARSET_UNDETECTED:
      logger.debug ('terminal charset: %s\r\n' % (self.client.char))
      return
    logger.debug ('request-terminal-charset')
    self.client.request_do_charset ()
    self.client.socket_send() #push
    t = time.time()
    while self.client.charset == self.CHARSET_UNDETECTED \
      and self.client._check_reply_pending(CHARSET) \
      and self._timeleft(t):
        time.sleep (self.TIME_POLL)
    if self.client.charset != self.CHARSET_UNDETECTED:
      logger.debug ('terminal charset: %s (negotiated)' %
          (self.client.charset,))
      return
    logger.debug ('failed: negotiate about character encoding')

    # set to cfg .ini if not detected
    self.client.charset = ini.cfg.get('session', 'default_encoding')
    logger.debug ('terminal charset: %s (default)' % (self.client.charset,))


  def _try_ttype(self):
    """Negotiate terminal type (TTYPE) telnet option (on)."""
    from bbs import ini
    detected = lambda: self.client.terminal_type != self.TTYPE_UNDETECTED
    if detected():
      logger.debug ('terminal type: %s (unsolicited)' %
          (self.client.terminal_type,))
      return
    logger.debug ('request-terminal-type')
    self.client.request_ttype ()
    self.client.socket_send() # push
    t = time.time()
    while not detected() and self._timeleft(t):
      time.sleep (self.TIME_POLL)
    if detected():
      logger.debug ('terminal type: %s (negotiated)' %
          (self.client.terminal_type,))
      return
    logger.debug ('failed: terminal type not determined.')

    # Try #2 - ... this is bullshit
    logger.debug('request answerback sequence')
    #self.client.request_wont_echo () # tell the client we will not echo,
    #                                 # on the other hand..., who cares?
    #self.client.socket_send () # push
    self.client.get_input () # flush & toss input
    self.client.send_str ('\005')  # send request termtype
    self.client.socket_send () # push
    t= time.time()
    while not self.client.input_ready() and self._timeleft(t):
      time.sleep (self.TIME_POLL)
    inp=''
    if self.client.input_ready():
      t = time.time()
      while self.client.idle() < self.TIME_PAUSE and self._timeleft(t):
          time.sleep (self.TIME_POLL)
      inp = self.client.get_input().lower()
      self.client.terminal_type = inp.strip()
      logger.debug ('terminal type: %s (answerback)' \
          % (self.client.terminal_type,))
      self.client.request_wont_echo ()
      return
    logger.debug ('failed: answerback reply not receieved')
    # set to cfg .ini if not detected
    self.client.terminal_type = ini.cfg.get('session', 'default_ttype')
    logger.debug ('terminal type: %s (default)' % (self.client.terminal_type,))


class POSHandler(threading.Thread):
  """
  This thread requires a client pipe, The telnet terminal is queried for its
  cursor position, and that position is sent as 'pos' event to the child pipe,
  otherwise a ('pos', None) is sent if no cursor position is reported within
  TIME_PAUSE.
  """
  TIME_POLL = 0.01
  TIME_PAUSE = 1.75
  TIME_WAIT = 1.30
  def __init__(self, pipe, client, lock, event='pos', timeout=None):
    self.pipe = pipe
    self.client = client
    self.lock = lock
    self.event = event
    self.TIME_WAIT = timeout if timeout is not None else self.TIME_WAIT
    threading.Thread.__init__ (self)

  def _timeleft(self, t):
    """Returns True when difference of current time and t is below TIME_WAIT"""
    return bool(time.time() -t < self.TIME_WAIT)

  def run(self):
    logger.debug ('getpos?')
    for (ttype, seq, pattern) in ConnectTelnetTerminal.WINSIZE_TRICK:
      self.lock.acquire ()
      self.client.send_str (seq)
      self.client.socket_send() # push
      t = time.time()
      while self.client.idle() < self.TIME_PAUSE and self._timeleft(t):
        time.sleep (self.TIME_POLL)
      inp = self.client.get_input()
      self.lock.release ()
      match = P.search (inp)
      logger.debug ('x %r/%d', inp, len(inp),)
      if not match and len(inp):
        # holy crap, this isn't for us ;^)
        self.pipe.send (('input', inp))
        continue
      elif match:
        row, col = match.groups()
        logger.debug ('xmit %s,%s', row, col)
        self.pipe.send (('pos', ((int(row)-1), int(col)-1,)))
        return
    self.pipe.send (('pos', (None, None,)))
    return
