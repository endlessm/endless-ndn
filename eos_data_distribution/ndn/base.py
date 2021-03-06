# -*- Mode:python; coding: utf-8; c-file-style:"gnu"; indent-tabs-mode:nil -*- */
#
# Copyright (C) 2016 Endless Mobile, Inc.
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

import logging
from os import path
from functools import partial

import gi
gi.require_version('GLib', '2.0')

from gi.repository import GObject
from gi.repository import GLib

from pyndn.security import KeyChain
from pyndn.transport.unix_transport import UnixTransport
from pyndn import Name, Node, Data, Face, Interest, InterestFilter, ControlParameters

from . import command
from utils import singleton

logger = logging.getLogger(__name__)


class GLibUnixTransport(UnixTransport):
    _watch_id = 0

    def connect(self, connectionInfo, elementListener, onConnected):
        super(GLibUnixTransport, self).connect(
            connectionInfo, elementListener, onConnected)

        fd = self._socket.fileno()
        io_channel = GLib.IOChannel.unix_new(fd)
        self._watch_id = GLib.io_add_watch(
            io_channel, GLib.PRIORITY_DEFAULT, GLib.IO_IN, self._socket_ready)

    def _socket_ready(self, channel, cond):
        nBytesRead = self._socket.recv_into(self._buffer)
        if nBytesRead <= 0:
            # Since we checked for data ready, we don't expect this.
            return

        self._elementReader.onReceivedData(self._bufferView[0:nBytesRead])
        return GLib.SOURCE_CONTINUE

    def close(self):
        super(GLibUnixTransport, self).close()

        if self._watch_id != 0:
            GLib.source_remove(self._watch_id)
            self._watch_id = 0


class GLibUnixFace(Face):

    def __init__(self):
        transport = GLibUnixTransport()
        file_path = self._getUnixSocketFilePathForLocalhost()
        connection_info = UnixTransport.ConnectionInfo(file_path)

        self._node = Node(transport, connection_info)
        self._commandKeyChain = None
        self._commandCertificateName = Name()

    @property
    def usesGLibMainContext(self):
        """
        Indicator that this Face implementation uses a GLib main context.

        This means it can be safely used in situations where the main context
        is the poll implementation, and progress would not be made otherwise
        without calling ``Face.processEvents()``.
        """
        return True

@singleton
def get_default_face():
    return GLibUnixFace()


def generate_keys(name):
    # Use the system default key chain and certificate name to sign commands.
    keyChain = KeyChain()
    try:
        certificateName = keyChain.getDefaultCertificateName()
    except:
        logger.warning(
            "Could not get default certificate name, creating a new one from %s", name)
        certificateName = keyChain.createIdentityAndCertificate(name)
    return (keyChain, certificateName)


class Base(GObject.GObject):

    def __init__(self, name, face=None):
        GObject.GObject.__init__(self)
        self.name = Name(name)

        # GLibUnixFace is the only Face implementation to do things in a GLib
        # main loop, so we require it.
        self.face = face or get_default_face()
        assert getattr(self.face, 'usesGLibMainContext', False)

        self._callbackCount = 0
        self._responseCount = 0
        self._keyChain = None
        self._certificateName = None
        self.pit = dict()

    def generateKeys(self, name=None):
        if not name:
            name = self.name
        (self._keyChain, self._certificateName) = generate_keys(name)
        self.face.setCommandSigningInfo(self._keyChain, self._certificateName)

    def sign(self, data):
        return self._keyChain.sign(data, self._certificateName)

    def expressInterest(self, interest=None, *args, **kwargs):
        if interest is None:
            try:
                interest = self.interest
            except AttributeError:
                interest = Interest(self.name)
        return self._expressInterest(interest, *args, **kwargs)

    def _expressInterest(self, interest, try_again=False,
                         onData=None, onTimeout=None):
        if not onData:
            onData = self._onData
        if not onTimeout:
            onTimeout = partial(self.onTimeout, try_again=try_again)

        logger.debug("Express Interest name: %s", interest)
        self.pit[interest] = self.face.expressInterest(
            interest, onData, onTimeout)
        return interest

    def expressCommandInterest(self, cmd, prefix=None,
                               onFailed=None, onTimeout=None, onSuccess=None,
                               *args, **kwargs):
        if not prefix and self.name:
            prefix = self.name
        if prefix:
            assert type(prefix) is Name

        interest = self._makeCommandInterest(
            cmd, prefix=prefix, *args, **kwargs)
        response = command._CommandResponse(prefix, face=self.face,
                                            onFailed=onFailed, onSuccess=onSuccess)
        return self._expressInterest(interest, prefix,
                                     onData=response.onData, onTimeout=response.onTimeout)

    def _makeCommandInterest(self, cmd, prefix=None, controlParameters=None,
                             keyChain=None, certificateName=None,
                             *args, **kwargs):

        if not controlParameters:
            controlParameters = ControlParameters()
        if not self._keyChain or not self._certificateName:
            self.generateKeys()

        if not keyChain:
            keyChain = self._keyChain
        if not certificateName:
            certificateName = self._certificateName

        controlParameters.setName(prefix)
        return command.makeInterest(cmd, controlParameters=controlParameters,
                                    keyChain=keyChain,
                                    certificateName=certificateName,
                                    local=self.face.isLocal(),
                                    *args, **kwargs)


class Producer(Base):
    __gsignals__ = {
        'register-failed': (GObject.SIGNAL_RUN_FIRST, None, (object, )),
        'register-success': (GObject.SIGNAL_RUN_FIRST, None, (object, object)),
        'interest': (GObject.SIGNAL_RUN_FIRST, None, (object, object, object, object, object))
    }

    def __init__(self, name=None, cost=None, *args, **kwargs):
        self.cost = cost

        super(Producer, self).__init__(name=name, *args, **kwargs)

        self.generateKeys()
        self._prefixes = dict()

    def start(self):
        self.registerPrefix()

    def send(self, name, content, flags = {}):
        data = Data(name)
        data.setContent(content)
        metadata = data.getMetaInfo()
        [getattr(metadata, 'set' + flag.capitalie())(arg) for (flag, arg) in flags]
        logger.debug('sending: %d, on %s', content.__len__(), name)
        self.sendFinish(data)
        return name

    def sendFinish(self, data):
        # self.sign(data)
        logger.debug('sending data: %s', data.getName())
        self.face.putData(data)

    def _onInterest(self, *args, **kwargs):
        self._responseCount += 1
        logger.debug("Got interest %s, %s", args, kwargs)

        self.emit('interest', *args, **kwargs)

    def registerPrefix(self, prefix=None, flags=None, *args, **kwargs):
        if prefix is None:
            prefix = self.name

        logger.info("Register prefix: %s", prefix)
        self._prefixes[prefix] = self._registerPrefix(
            prefix, flags, *args, **kwargs)
        return prefix

    def _registerPrefix(self, prefix, cost=None, controlParameters=None,
                        onInterest=None, onRegisterFailed=None,
                        onRegisterSuccess=None,
                        *args, **kwargs):
        node = self.face._node
        registeredPrefixId = node.getNextEntryId()

        if not cost:
            cost = self.cost
        if not onInterest:
            onInterest = self._onInterest
        if not onRegisterFailed:
            onRegisterFailed = self.onRegisterFailed
        if not onRegisterSuccess:
            onRegisterSuccess = self.onRegisterSuccess

        if not controlParameters:
            controlParameters = ControlParameters()
        if cost:
            controlParameters.setCost(int(cost))

        def _addToRegisteredPrefixTable(prefix):
            # Success, so we can add to the registered prefix table.
            if registeredPrefixId != 0:
                interestFilterId = 0
                if onInterest != None:
                    # registerPrefix was called with the "combined" form
                    # that includes the callback, so add an
                    # InterestFilterEntry.
                    interestFilterId = node.getNextEntryId()
                    node.setInterestFilter(
                        interestFilterId, InterestFilter(prefix),
                      onInterest, self.face)

                if not node._registeredPrefixTable.add(
                        registeredPrefixId, prefix, interestFilterId):
                    # removeRegisteredPrefix was already called with the
                    # registeredPrefixId.
                    if interestFilterId > 0:
                        # Remove the related interest filter we just added.
                        node.unsetInterestFilter(interestFilterId)

                    return

            if onRegisterSuccess:
                onRegisterSuccess(prefix, registeredPrefixId)

        self.expressCommandInterest('/nfd/rib/register', prefix,
                                    controlParameters=controlParameters,
                                    onFailed=onRegisterFailed,
                                    onSuccess=_addToRegisteredPrefixTable)
        return registeredPrefixId

    def onRegisterFailed(self, prefix):
        self._responseCount += 1
        self.emit('register-failed', prefix)
        logger.warning("Register failed for prefix: %s", prefix)

    def onRegisterSuccess(self, prefix, registered):
        self.emit('register-success', prefix, registered)
        logger.info(
            "Register succeeded for prefix: %s, %s", prefix, registered)

    def removeRegisteredPrefix(self, prefix):
        name = Name(prefix)
        logger.info("Un-Register prefix: %s", name)
        try:
            self.face.removeRegisteredPrefix(self._prefixes[name])
            del(self._prefixes[name])
        except:
            logger.warning(
                "tried to unregister a prefix that never was registered: %s", prefix)
            pass


class Consumer(Base):
    __gsignals__ = {
        'data': (GObject.SIGNAL_RUN_FIRST, None, (object, object)),
        'interest-timeout': (GObject.SIGNAL_RUN_FIRST, None, (object, bool)),
    }

    def __init__(self, name=None, *args, **kwargs):
        super(Consumer, self).__init__(name=name, *args, **kwargs)

        #        self.generateKeys()
        self._prefixes = dict()

    def start(self):
        self.expressInterest()

    def _onData(self, interest, data):
        self._callbackCount += 1
        self.emit('data', interest, data)

    def removePendingInterest(self, name):
        self.face.removePendingInterest(self.pit[name])
        del self.pit[name]

    def onTimeout(self, interest, try_again=False):
        name = interest.getName()
        self._callbackCount += 1
        self.emit('interest-timeout', interest, try_again)
        logger.debug("Time out for interest: %s", name)
        if try_again:
            logger.info("Re-requesting Interest: %s", name)
            self._expressInterest(interest, try_again=try_again)
