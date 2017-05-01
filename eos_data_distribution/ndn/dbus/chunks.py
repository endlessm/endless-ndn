# -*- Mode:python; coding: utf-8; c-file-style:"gnu"; indent-tabs-mode:nil -*- */
#
# Copyright (C) 2017 Endless Mobile Inc.
# Author: Niv Sardi <xaiki@endlessm.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
# A copy of the GNU Lesser General Public License is in the file COPYING.

import errno
import json
import logging
import os
from shutil import copyfile
from os import path

import gi
gi.require_version('EosDataDistributionDbus', '0')
gi.require_version('GLib', '2.0')

from gi.repository import EosDataDistributionDbus
from gi.repository import GObject
from gi.repository import GLib
from gi.repository import Gio

from . import base
from eos_data_distribution import utils

logger = logging.getLogger(__name__)

# signals -> property notification
# kill temporal signal
# only multiplex on the object path

class Data(object):
    """Data:

    This mimics the NDN Data object, it should implement as little API as we
    need, we pass an fd that comes from the Consumer, and currently
    setContent is a hack that actually writes to the fd.
    we write here so that we don't have to cache big chunks of data in memory.

    """
    def __init__(self, fd, n = 0):
        super(Data, self).__init__()

        self.fd = fd
        self.n = n - 1

    def setContent(self, buf):
        cur_pos = self.fd.tell()
        n = self.n + 1

        assert(cur_pos/base.CHUNK_SIZE == n)

        # write directly to the fd, sendFinish is a NOP
        logger.debug('write data START: %d, fd: %d, buf: %d',
                     n, cur_pos, len(buf))
        ret = self.fd.write(buf)
        self.fd.flush()
        logger.debug('write data END: %d, fd: %d', n, self.fd.tell())
        self.n = n
        return ret


class Consumer(base.Consumer):

    def __init__(self, name, *args, **kwargs):
        self.filename = None
        self.fd = None

        self.first_segment = 0
        self.current_segment = 0
        self._final_segment = None
        self._num_segments = None
        self._segments = None
        self._qualified_name = None
        self._emitted_complete = False

        super(Consumer, self).__init__(name=name, *args, **kwargs)
        logger.info('init DBUS chunks.Consumer: %s', name)

    def _dbus_express_interest(self, interest, dbus_path, dbus_name):
        # XXX parse interest to see if we're requesting the first chunk
        self.first_segment = 0
        self.interest = interest

        assert(not self.filename)
        assert(not self.fd)

        # we prepare the file where we're going to write the data
        self.filename = '.edd-file-cache-' + interest.replace('/', '%')
        self.fd = open(self.filename, 'w+b')

        logger.info('calling on; %s %s', dbus_path, dbus_name)

        EosDataDistributionDbus.ChunksProducerProxy.new(
            self.con, Gio.DBusProxyFlags.NONE, dbus_name, dbus_path, None,
            self._on_proxy_ready)

    def _on_proxy_ready(self, proxy, res):
        self._proxy = EosDataDistributionDbus.ChunksProducerProxy.new_finish(res)
        self._proxy.connect('progress', self._on_progress)
        self._proxy.call_request_interest(self.interest,
                                          GLib.Variant('h', self.fd.fileno()),
                                          self.first_segment,
                                          None, self._on_call_complete)

    def _save_chunk(self, n, data):
        raise NotImplementedError()

    def _on_progress(self, proxy, name, first_segment, last_segment):
        logger.info('got progress, (%s) %s → %s', self.fd,  self.current_segment, last_segment)

        assert(self._final_segment)
        assert(first_segment <= self.current_segment)

        self.current_segment = max(self.current_segment, self.first_segment)
        self.fd.seek(self.current_segment * self.chunk_size)
        while (self.current_segment <= last_segment):
            progress = (float(self.current_segment) / (self._final_segment or 1)) * 100
            self.emit('progress', progress)
            logger.debug('consumer read segment: %s', self.current_segment)
            buf = self.fd.read(self.chunk_size)
            if not buf:
                # XXX should we retry ?
                logger.warning('consumer read segment FAILED: %s @ %s', self.current_segment, self.fd.tell())
                return

            self._save_chunk(self.current_segment, buf)
            self.current_segment += 1

        self.current_segment -= 1
        # XXX this would be self._check_for_complete()
        self._on_complete()

    def _on_call_complete(self, proxy, res):
        self._final_segment = proxy.call_request_interest_finish(res)
        logger.info('request interest complete: %s', self._final_segment)

    def _on_complete(self):
        logger.debug("COMPLETE: %s, %s", self.current_segment, self._final_segment)
        assert (self.current_segment == self._final_segment)
        self.emit('complete')
        os.unlink(self.fd.name)
        self.fd.close()
        logger.info('fully retrieved: %s', self.name)

class Producer(base.Producer):
    def __init__(self, name, *args, **kwargs):
        super(Producer, self).__init__(name=name,
                                       skeleton=EosDataDistributionDbus.ChunksProducerSkeleton(),
                                       *args, **kwargs)

    def _on_request_interest(self, skeleton, invocation, name, fd_variant, first_segment):
        fd = fd_variant.get_handle()
        logger.debug('RequestInterest: name=%s, fd=%d, first_segment=%d',
                     name, fd, first_segment)

        # do we start on chunk 0 ? full file ? do we start on another chunk
        # ? we need to seek the file, subsequent calls to get the same
        # chunks have to be handled in the consumer part and folded into
        # answering to only one dbus call

        final_segment = self._get_final_segment()
        if not final_segment:
            raise NotImplementedError()

        self._workers[name] = worker = ProducerWorker(fd, first_segment, final_segment,
                                                      self._send_chunk)
        skeleton.complete_request_interest(invocation, final_segment)

        # XXX: is this racy ?
        GLib.timeout_add_seconds(5,
            lambda: skeleton.emit_progress(name, worker.first_segment,
                                           worker.data.n) or True)

        return True

    def sendFinish(self, data):
        # we don't need to do anything here because we write the file in
        # setContent, we don't do it here because that would require us to
        # cache big chunks of data in memory.
        pass

class ProducerWorker():
    def __init__(self, fd, first_segment, final_segment, send_chunk):
        self.first_segment = first_segment
        self.current_segment = first_segment
        self.fd = os.fdopen(fd, 'w+b')
        self.data = Data(self.fd, first_segment)

        logger.info('start segments: %s, %s', self.current_segment, final_segment)
        while(True):
            send_chunk(self.data, self.current_segment)
            if self.current_segment < final_segment:
                self.current_segment += 1
            else:
                break

        logger.info('end segments: %s, %s', self.current_segment, final_segment)

if __name__ == '__main__':
    import re
    from .tests import utils as testutils
    from . import http

    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--cost", default=10)
    parser.add_argument("-o", "--output", default='test.shard')
    parser.add_argument("url")
    args = utils.parse_args(parser=parser)

    if args.name:
        name = args.name
    else:
        name = re.sub('https?://', '', args.url)

    consumer = http.Consumer(name=name, url=args.url)
    testutils.run_consumer_test(consumer, name, args)