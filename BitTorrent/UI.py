# The contents of this file are subject to the BitTorrent Open Source License
# Version 1.1 (the License).  You may not copy or use this file, in either
# source code or executable form, except in compliance with the License.  You
# may obtain a copy of the License at http://www.bittorrent.com/license/.
#
# Software distributed under the License is distributed on an AS IS basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied.  See the License
# for the specific language governing rights and limitations under the
# License.

# written by Matt Chisholm

from __future__ import division

import os
import os.path
import atexit
import itertools
import webbrowser
from copy import copy
import logging
import logging.handlers

from BitTorrent.translation import _

import BitTorrent.stackthreading as threading
from BitTorrent import GetTorrent
from BitTorrent import LaunchPath
from BitTorrent.MultiTorrent import UnknownInfohash, TorrentAlreadyInQueue, TorrentAlreadyRunning, TorrentNotRunning
from BitTorrent.obsoletepythonsupport import set
from BitTorrent.yielddefer import launch_coroutine
from BitTorrent.defer import ThreadedDeferred
from BitTorrent.platform import desktop
from BitTorrent.Torrent import *
from BitTorrent.ThreadProxy import ThreadProxy


class Size(long):
    """displays size in human-readable format"""

    size_labels = ['','K','M','G','T','P','E','Z','Y']
    radix = 2**10

    def __new__(cls, value=None, precision=None):
        if value is None:
            self = long.__new__(cls, 0)
            self.empty = True
        else:
            self = long.__new__(cls, value)
            self.empty = False
        return self

    def __init__(self, value, precision=0):
        long.__init__(self, value)
        self.precision = precision

    def __str__(self, precision=None):
        if self.empty:
            return ''
        if precision is None:
            precision = self.precision
        value = self
        for unitname in self.size_labels:
            if value < self.radix and precision < self.radix:
                break
            value /= self.radix
            precision /= self.radix
        if unitname and value < 10 and precision < 1:
            return '%.1f %sB' % (value, unitname)
        else:
            return '%.0f %sB' % (value, unitname)


class Rate(Size):
    """displays rate in human-readable format"""

    def __init__(self, value, precision=2**10):
        Size.__init__(self, value, precision)

    def __str__(self, precision=2**10):
        if self.empty:
            return ''
        return '%s/s'% Size.__str__(self, precision=precision)


class Duration(float):
    """displays duration in human-readable format"""

    def __new__(cls, value=None):
        if value == None:
            self = float.__new__(cls, 0)
            self.empty = True
        else:
            self = float.__new__(cls, value)
            self.empty = False
        return self

    def __str__(self):
        if self.empty or self > 365 * 24 * 60 * 60:
            return '?'
        elif self >= 172800:
            return _("%d days") % (self//86400) # 2 days or longer
        elif self >= 86400:
            return _("1 day %d hours") % ((self-86400)//3600) # 1-2 days
        elif self >= 3600:
            return _("%d:%02d hours") % (self//3600, (self%3600)//60) # 1 h - 1 day
        elif self >= 60:
            return _("%d:%02d minutes") % (self//60, self%60) # 1 minute to 1 hour
        elif self >= 0:
            return _("%d seconds") % int(self)
        else:
            return _("0 seconds")


def ip_sort(a_str,b_str):
    """Fast IP address sorting function"""
    for a,b in itertools.izip(a_str.split('.'), b_str.split('.')):
        if a == b:
            continue
        if len(a) == len(b):
            return cmp(a,b)
        return cmp(int(a), int(b))
    return 0


def find_dir(path):
    if os.path.isdir(path):
        return path
    directory, garbage = os.path.split(path)
    while directory:
        if os.access(directory, os.F_OK) and os.access(directory, os.W_OK):
            return directory
        directory, garbage = os.path.split(directory)
        if garbage == '':
            break
    return None

def smart_dir(path):
    path = find_dir(path)
    if path is None:
        path = desktop
    return path

if os.name == 'nt':
    disk_term = _("drive")
elif os.name == 'posix' and os.uname()[0] == 'Darwin':
    disk_term = _("volume")
else:
    disk_term = _("disk")


class BasicTorrentObject(object):
    """Object for holding all information about a torrent"""

    def __init__(self, torrent, completion):
        self.torrent = torrent
        self.pending = None
        self.infohash   = torrent.metainfo.infohash
        self.metainfo   = torrent.metainfo
        self.destination_path = torrent.destination_path
        self.working_path     = torrent.working_path

        self.state = torrent.state
        self.policy = torrent.policy
        self.completed = torrent.completed
        self.priority = torrent.priority
        self.completion = completion
        self.piece_states = None

        self.uptotal    = 0
        self.downtotal  = 0
        self.up_down_ratio = 0
        self.peers = 0

        self.handler = logging.handlers.MemoryHandler(0) # capacity is ignored
        logging.getLogger("core.MultiTorrent." + repr(self.infohash)).addHandler(self.handler)


    def update(self, state, policy, completed, statistics):
        self.state = state
        self.policy = policy
        self.completed = completed
        self.priority = statistics['priority']
        self.completion = statistics['fractionDone']
        self.piece_states = statistics['pieceStates']

        self.uptotal   += statistics.get('upTotal'  , 0)
        self.downtotal += statistics.get('downTotal', 0)
        try:
            self.up_down_ratio = self.uptotal / self.torrent.metainfo.total_bytes
        except ZeroDivisionError:
            self.up_down_ratio = 0

        self.peers = statistics.get('numPeers', 0)


    def wants_peers(self):
        return True


    def wants_files(self):
        return self.metainfo.is_batch


    def clean_up(self):
        logging.getLogger("core.MultiTorrent." + repr(self.infohash)).removeHandler(self.handler)



class BasicApp(object):
    torrent_object_class = BasicTorrentObject

    def __init__(self, config):
        self.started = 0
        self.multitorrent = None
        self.config = config
        self.torrents = {}
        self.external_torrents = []
        self.installer_to_launch_at_exit = None
        self.logger = logging.getLogger('UI')

        def gui_wrap(_f, *args, **kwargs):
            f(*args, **kwargs)

        self.gui_wrap = gui_wrap
        self.open_external_torrents_deferred = None


    def quit(self):
        if self.doneflag:
            self.doneflag.set()


    def visit_url(self, url, callback=None):
        """Visit a URL in the user's browser"""
        t = threading.Thread(target=self._visit_url, args=(url, callback))
        t.start()


    def _visit_url(self, url, callback=None):
        """Visit a URL in the user's browser non-blockingly"""
        webbrowser.open(url)
        if callback:
            self.gui_wrap(callback)


    def open_torrent_arg(self, path):
        """Open a torrent from path (URL, file) non-blockingly"""
        df = ThreadedDeferred(self.gui_wrap, GetTorrent.get_quietly, path)
        return df


    def publish_torrent(self, torrent, publish_path):
        df = self.open_torrent_arg(torrent)
        yield df
        try:
            metainfo = df.getResult()
        except GetTorrent.GetTorrentException:
            self.logger.exception("publish_torrent failed")
            return
        df = self.multitorrent.create_torrent(metainfo, publish_path, publish_path)
        yield df
        df.getResult()


    def open_torrent_arg_with_callbacks(self, path):
        """Open a torrent from path (URL, file) non-blockingly, and
        call the appropriate GUI callback when necessary."""
        def callback(m):
            self.gui_wrap(self.open_torrent_metainfo, m)
        def  errback(e):
            c,e,t = e
            if issubclass(c, GetTorrent.GetTorrentException):
                self.logger.critical(e)
            else:
                self.logger.error("open_torrent_arg_with_callbacks failed", exc_info=e)
        df = self.open_torrent_arg(path)
        df.addCallback(callback)
        df.addErrback(errback)
        return df


    def append_external_torrents(self, *a):
        """Append external torrents (such as those specified on the
        command line) so that they can be processed (for save paths,
        error reporting, etc.) once the GUI has started up."""
        self.external_torrents.extend(a)


    def _open_external_torrents(self):
        """Open torrents added externally (on the command line before
        startup) in a non-blocking yet serial way."""
        while self.external_torrents:
            arg = self.external_torrents.pop(0)
            df = self.open_torrent_arg(arg)
            yield df

            try:
                metainfo = df.getResult()
            except GetTorrent.GetTorrentException:
                self.logger.exception("Failed to get torrent")
                continue

            if metainfo is not None:
                # metainfo may be none if IE passes us a path to a
                # file in its cache that has already been deleted
                # because it came from a website which set
                # Pragma:No-Cache on it.
                # See GetTorrent.get_quietly().

                df = self.multitorrent.torrent_known(metainfo.infohash)
                yield df
                known = df.getResult()
                if known:
                    # BUG: we should probably report this?
                    continue
                
                df = self.open_torrent_metainfo(metainfo)
                if df is not None:
                    yield df
                    try:
                        df.getResult()
                    except TorrentAlreadyInQueue:
                        pass
                    except TorrentAlreadyRunning:
                        pass
        self.open_external_torrents_deferred = None


    def open_external_torrents(self):
        """Open torrents added externally (on the command line before startup)."""
        if self.open_external_torrents_deferred is None and \
               len(self.external_torrents):
            self.open_external_torrents_deferred = launch_coroutine(self.gui_wrap, self._open_external_torrents)
            def callback(*a):
                self.open_external_torrents_deferred = None
            def errback(e):
                callback()
                self.logger.error("open_external_torrents failed:", exc_info=e)

            self.open_external_torrents_deferred.addCallback(callback)
            self.open_external_torrents_deferred.addErrback(errback)


    def open_torrent_metainfo(self, metainfo):
        """Get a valid save path from the user, and then tell
        multitorrent to create a new torrent from metainfo."""
        raise NotImplementedError('BasicApp.open_torrent_metainfo() not implemented')


    def launch_torrent(self, infohash):
        """Launch the torrent contents according to operating system."""
        if self.torrents.has_key(infohash):
            torrent = self.torrents[infohash]
            if torrent.metainfo.is_batch:
                LaunchPath.launchdir(torrent.working_path)
            else:
                LaunchPath.launchfile(torrent.working_path)


    def launch_installer_at_exit(self):
        LaunchPath.launchfile(self.installer_to_launch_at_exit)


    def do_log(self, severity, text):
        raise NotImplementedError('BasicApp.do_log() not implemented')

    def init_updates(self):
        """Make status request at regular intervals."""
        raise NotImplementedError('BasicApp.init_updates() not implemented')


    def make_statusrequest(self, event = None):
        """Make status request."""
        # BUG: we're not handling errors here.
        launch_coroutine(self.gui_wrap, self.update_status)
        return True


    def update_status(self):
        """Update torrent information based on the results of making a
        status request."""
        df = self.multitorrent.get_torrents()
        yield df
        torrents = df.getResult()

        infohashes = set()

        for torrent in torrents:
            torrent = ThreadProxy(torrent, self.gui_wrap)
            infohashes.add(torrent.metainfo.infohash)
            if torrent.metainfo.infohash not in self.torrents:
                # create new torrent widget
                completion = None
                to = self.new_displayed_torrent(torrent, completion)

        for infohash, torrent in copy(self.torrents).iteritems():
            # remove nonexistent torrents
            if infohash not in infohashes:
                self.torrents.pop(infohash)
                self.torrent_removed(infohash)

        total_completion = 0
        total_bytes = 0

        for infohash, torrent in copy(self.torrents).iteritems():
            # update existing torrents
            df = self.multitorrent.torrent_status(infohash,
                                                  torrent.wants_peers(),
                                                  torrent.wants_files()
                                                  )
            yield df
            try:
                state, policy, completed, statistics = df.getResult()
            except UnknownInfohash:
                # looks like it's gone now
                if infohash in self.torrents:
                    self.torrents.pop(infohash)
                    self.torrent_removed(infohash)
            else:
                # the infohash might have been removed from torrents
                # while we were yielding above, so we need to check
                if infohash in self.torrents:
                    torrent.update(state, policy, completed, statistics)
                    self.update_torrent(infohash, state, policy, completed, statistics)
                    if statistics['fractionDone'] is not None:
                        amount_done = statistics['fractionDone'] * torrent.metainfo.total_bytes
                        total_completion += amount_done
                        total_bytes += torrent.metainfo.total_bytes
        if total_bytes == 0:
            average_completion = 0
        else:
            average_completion = total_completion / total_bytes

        df = self.multitorrent.auto_update_status()
        yield df
        available_version, installable_version = df.getResult()
        if available_version is not None:
            if installable_version is None:
                self.notify_of_new_version(available_version)
            else:
                if self.installer_to_launch_at_exit is None:
                    atexit.register(self.launch_installer_at_exit)
                torrent = self.torrents[installable_version]
                self.installer_to_launch_at_exit = torrent.working_path
                self.prompt_for_quit_for_new_version(available_version)

        def get_global_stats(mt):
            stats = {}

            u, d = mt.get_total_rates()
            stats['total_uprate'] = Rate(u)
            stats['total_downrate'] = Rate(d)

            u, d = mt.get_total_totals()
            stats['total_uptotal'] = Size(u)
            stats['total_downtotal'] = Size(d)

            stats['num_torrents'] = len(mt.torrents)
            stats['num_running_torrents'] = len(mt.running)

            stats['num_connections'] = 0
            for t in mt.get_torrents():
                stats['num_connections'] += t.get_num_connections()

            try:
                stats['avg_connections'] = (stats['num_connections'] /
                                            stats['num_running_torrents'])
            except ZeroDivisionError:
                stats['avg_connections'] = 0

            stats['avg_connections'] = "%.02f" % stats['avg_connections']

            return stats

        df = self.multitorrent.call_with_obj(get_global_stats)
        yield df
        global_stats = df.getResult()

        yield average_completion, global_stats


    def _update_status(self, total_completion):
        raise NotImplementedError('BasicApp._update_status() not implemented')


    def new_displayed_torrent(self, torrent, completion):
        """Tell the UI that it should draw a new torrent."""
        torrent_object = self.torrent_object_class(torrent, completion)
        self.torrents[torrent.metainfo.infohash] = torrent_object
        return torrent_object

    def torrent_removed(self, infohash):
        """Tell the GUI that a torrent has been removed, by it, or by
        multitorrent."""
        raise NotImplementedError('BasicApp.torrent_removed() removing missing torrents not implemented')


    def update_torrent(self, infohash, state, policy, completed, statistics):
        """Tell the GUI to update a torrent's info."""
        raise NotImplementedError('BasicApp.update_torrent() updating existing torrents not implemented')

    def notify_of_new_version(self, version):
        print 'got auto_update_status', version
        pass

    def prompt_for_quit_for_new_version(self, version):
        print 'got new version', version
        pass

    # methods that are used to send commands to MultiTorrent
    def send_config(self, option, value, infohash=None):
        """Tell multitorrent to set a config item."""
        self.config[option] = value
        if self.multitorrent:
            self.multitorrent.set_config(option, value, infohash)


    def remove_infohash(self, infohash):
        """Tell multitorrent to remove a torrent."""
        df = self.multitorrent.remove_torrent(infohash)
        yield df
        self.torrent_removed(infohash)
        tw = self.torrents.pop(infohash)
        tw.clean_up()


    def set_file_priority(self, infohash, filenames, dowhat):
        """Tell multitorrent to set file priorities."""
        for f in filenames:
            self.multitorrent.set_file_priority(infohash, f, dowhat)


    def stop_torrent(self, infohash, pause=False):
        """Tell multitorrent to stop a torrent."""
        torrent = self.torrents[infohash]
        if (torrent and torrent.pending == None):
            torrent.pending = "stop"

            if not pause:
                df = self.multitorrent.set_torrent_policy(infohash, "stop")
                yield df
                try:
                    df.getResult()
                except TorrentNotRunning:
                    pass

            if torrent.state == "running":
                df = self.multitorrent.stop_torrent(infohash)
                yield df
                torrent.state = df.getResult()

            torrent.pending = None


    def start_torrent(self, infohash):
        """Tell multitorrent to start a torrent."""
        torrent = self.torrents[infohash]
        if (torrent and torrent.pending == None and
            torrent.state in ["failed", "initialized"]):
            torrent.pending = "start"

            if torrent.state == "failed":
                df = self.multitorrent.reinitialize_torrent(infohash)
                yield df
                df.getResult()

            df = self.multitorrent.set_torrent_policy(infohash, "auto")
            yield df
            df.getResult()

            torrent.pending = None


    def force_start_torrent(self, infohash):
        torrent = self.torrents[infohash]
        if (torrent and torrent.pending == None):
            torrent.pending = "force start"

            df = self.multitorrent.set_torrent_policy(infohash, "start")
            yield df
            df.getResult()

            if torrent.state in ["failed", "initialized"]:
                if torrent.state == "failed":
                    df = self.multitorrent.reinitialize_torrent(infohash)
                    yield df
                    df.getResult()

                df = self.multitorrent.start_torrent(infohash)
                yield df
                torrent.state = df.getResult()

            torrent.pending = None


    def external_command(self, action, *datas):
        """For communication via IPC"""
        if action == 'start_torrent':
            assert len(datas) == 1
            self.append_external_torrents(*datas)
            self.logger.info('got external_command:start_torrent: "%s"' % datas[0])
            # this call does Ye Olde Threadede Deferrede:
            self.open_external_torrents()
        elif action == 'publish_torrent':
            self.logger.info('got external_command:publish_torrent: "%s" as "%s"' % datas)
            launch_coroutine(self.gui_wrap, self.publish_torrent, datas[0], datas[1])
        elif action == 'show_error':
            assert len(datas) == 1
            self.logger.error(datas[0])
        elif action == 'no-op':
            self.logger.info('got external_command: no-op')
        else:
            self.logger.warning('got unknown external_command: %s' % str(action))
            # fun.
            #code = action + ' '.join(datas)
            #self.logger.warning('eval: %s' % code)
            #exec code
