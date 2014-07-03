"""
x/84 bbs module, https://github.com/jquast/x84
"""
from x84 import encodings
from x84.bbs.userbase import list_users, get_user, find_user, User, Group
from x84.bbs.msgbase import list_msgs, get_msg, list_tags, Msg
from x84.bbs.exception import Disconnected, Goto
from x84.bbs.editor import LineEditor, ScrollingEditor
from x84.bbs.output import (echo, timeago, encode_pipe, decode_pipe,
                            syncterm_setfont,
                            Ansi, ansiwrap  # deprecated
                            )
from x84.bbs.ansiwin import AnsiWindow
from x84.bbs.selector import Selector
from x84.bbs.lightbar import Lightbar
from x84.bbs.cp437 import from_cp437
from x84.bbs.dbproxy import DBProxy
from x84.bbs.pager import Pager
from x84.bbs.door import Door, DOSDoor, Dropfile


__all__ = ['list_users', 'get_user', 'find_user', 'User', 'Group', 'list_msgs',
           'get_msg', 'list_tags', 'Msg', 'LineEditor', 'ScrollingEditor',
           'echo', 'timeago', 'Ansi', 'ansiwrap', 'AnsiWindow', 'Selector',
           'Lightbar', 'from_cp437', 'DBProxy', 'Pager', 'Door', 'DOSDoor',
           'goto', 'disconnect', 'getsession', 'getterminal', 'getch', 'gosub',
           'ropen', 'showart', 'showcp437', 'Dropfile', 'encode_pipe',
           'decode_pipe', 'syncterm_setfont',
           ]


def goto(*args):
    """
    Change bbs script. Does not return.
    """
    raise Goto(args)


def disconnect(reason=u''):
    """
    Disconnect session. Does not return.
    """
    raise Disconnected(reason,)


def getsession():
    """
    Returns Session of calling process.
    """
    import x84.bbs.session
    return x84.bbs.session.getsession()


def getterminal():
    """
    Returns Terminal of calling process.
    """
    import x84.bbs.session
    return x84.bbs.session.getterminal()


# temporary hacks until blessings updates with term.inkey() upstream ..
def getch(timeout=None):
    """
    Retrieve a keystroke from 'input' queue, blocking forever or, when
    specified, None when timeout has elapsed.

    upstream blessings has better 'keycode' evaluation (none of this
    duck typing, its always unicode, but has .is_sequence bool test,
    and a .value test for keycode comparison). we workaround for legacy
    behavior unless upstream blessings accepts our impl. in some form ..
    """
    keystroke = getterminal().inkey(timeout)
    if keystroke == u'':
        return None
    if keystroke.is_sequence:
        return keystroke.code
    return keystroke


def gosub(*arg):
    """
    Call bbs script with optional arguments, Returns value.
    """
    # pylint: disable=W0142
    #        Used * or ** magic
    return getsession().runscript(*(arg[0],) + arg[1:])


def ropen(filename, mode='rb'):
    """
    Open random file using wildcard (glob)
    """
    import glob
    import random
    files = glob.glob(filename)
    return open(random.choice(files), mode) if len(files) else None


def showart(filepattern, encoding=None, auto_mode=True):
    """
    yield unicode sequences for any given ANSI Art (of art_encoding). Effort
    is made to strip SAUCE data, translate input to unicode, and trim artwork
    too large to display. If keyboard input is pressed, 'msg_cancel' is
    returned as the last line of art.

    Alternate codecs are available if you provide the ``encoding`` argument. Ie
    if you want to show an Amiga style ASCII art file::

        >>> from x84.bbs import echo, showart
        >>> for line in showart('test.asc', 'topaz'):
        ...     echo(line)

    The ``auto_mode`` flag will, if set, only respect the selected encoding if
    the active session is UTF-8 capable.

    """
    if encoding is None:
        from x84.bbs.ini import CFG
        try:
            encoding = CFG.get('system', 'art_utf8_codec')
        except:
            encoding = 'cp437'  # Default fallthrough

    import sauce
    session, term = getsession(), getterminal()
    msg_cancel = u''.join((term.normal,
                           term.bold_blue(u'--'),
                           u'CANCEllEd bY iNPUt ',
                           term.bold_blue(u'--'),))
    msg_notfound = u''.join((
        term.bold_red(u'--'),
        u'no files matching %s' % (filepattern,),
        term.bold_red(u'--'),))
    fobj = (ropen(filepattern)
            if '*' in filepattern or '?' in filepattern
            else open(filepattern, 'rb'))

    if fobj is None:
        yield msg_notfound + u'\r\n'
        return

    if auto_mode:
        def _decode(what):
            session = getsession()
            if session.encoding == 'utf8':
                return what.decode(encoding)
            elif session.encoding == 'cp437':
                return what.decode('cp437')
            else:
                return what

    else:
        _decode = lambda what: what.decode(encoding)

    for line in _decode(str(sauce.SAUCE(fobj))).splitlines():
        # allow slow terminals to cancel by pressing a keystroke
        if session.poll_event('input'):
            yield u'\r\n' + msg_cancel + u'\r\n'
            return
        yield line + u'\r\n'


def showcp437(filepattern):
    """
    Alias for the :func:`showart` function, with the ``cp437`` codec as
    default.
    """
    return showart(filepattern, 'cp437')
