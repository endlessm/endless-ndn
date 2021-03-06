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

import argparse
import logging

from pyndn.control_parameters import ControlParameters
from pyndn.control_response import ControlResponse
from pyndn.encoding.tlv_wire_format import TlvWireFormat
from pyndn.util.command_interest_generator import CommandInterestGenerator
from pyndn import Interest, Name

from eos_data_distribution import utils

logger = logging.getLogger(__name__)

_commandInterestGenerator = CommandInterestGenerator()


class _CommandResponse(object):

    """
    A _CommandResponse receives the response Data packet from a
    command interest sent to the connected NDN hub. If this gets a bad
    response or a timeout, call onFailed.

    this is adapted from PyNDN2's _RegisterResponse object
    """

    def __init__(self, prefix, onFailed, onSuccess, face):
        self._prefix = prefix
        self._onFailed = onFailed
        self._onSuccess = onSuccess
        self._face = face

    def onData(self, interest, responseData):
        """
        We received the response.
        """
        # Decode responseData.getContent() and check for a success code.
        controlResponse = ControlResponse()
        try:
            controlResponse.wireDecode(
                responseData.getContent(), TlvWireFormat.get())
        except ValueError as ex:
            logger.info(
                "command failed: Error decoding the NFD response: %s",
              str(ex))
            try:
                self._onFailed(self._prefix)
            except:
                logging.exception("Error in onFailed")
            return

        # Status code 200 is "OK".
        if controlResponse.getStatusCode() != 200:
            logger.info(
                "command failed: Expected NFD status code 200, got: %d: %s",
                controlResponse.getStatusCode(), controlResponse.getStatusText())
            try:
                if self._onFailed:
                    self._onFailed(self._prefix)
            except:
                logging.exception("Error in onFailed")
            return

        logger.info(
            "command succeeded with the NFD forwarder for prefix %s",
          self._prefix.toUri())
        if self._onSuccess != None:
            try:
                self._onSuccess(self._prefix)
            except:
                logging.exception("Error in onSuccess")

    def onTimeout(self, interest):
        """
        We timed out waiting for the response.
        """
        logger.info("Timeout for NFD command.")
        try:
            self._onFailed(self._prefix)
        except:
            logging.exception("Error in onFailed")


def generateInterest(*args, **kwargs):
    logger.debug("args kwargs: %s %s", args, kwargs)
    _commandInterestGenerator.generate(*args, **kwargs)


def dumpControlParameter(cp):
    return ("""
    Control Parameter dump:
    name:     %s
    faceId:   %s
    uri:      %s
    localControlfeature: %s
    origin:   %s
    cost:     %s
    forwardingFlags:     %s
    strategy: %s
    expirationPeriod:    %s
""" % (
            cp._name,
            cp._faceId,
            cp._uri,
            cp._localControlFeature,
            cp._origin,
            cp._cost,
            cp._forwardingFlags,
            cp._strategy,
            cp._expirationPeriod
            ))


def makeInterest(cmd, local=True, controlParameters={}, interestLifeTime=None,
                 keyChain=None, certificateName=None):
    assert(cmd.startswith('/'))

    commandInterest = Interest()
    if local:  # the values of the timeouts come from the default PyNDN implementation
        commandInterest.setName(Name("/localhost%s" % cmd))
        # The interest is answered by the local host, so set a short timeout.
        commandInterest.setInterestLifetimeMilliseconds(2000.0)
    else:
        commandInterest.setName(Name("/localhop%s" % cmd))
        # The host is remote, so set a longer timeout.
        commandInterest.setInterestLifetimeMilliseconds(4000.0)
    if interestLifeTime:
        commandInterest.setInterestLifetimeMilliseconds(interestLifeTime)
    # NFD only accepts TlvWireFormat packets.

    logger.debug("DUMP: %s", dumpControlParameter(controlParameters))
    logger.debug("wire encoding: %s", str(
        controlParameters.wireEncode(TlvWireFormat.get())).encode('quopri'))

    commandInterest.getName().append(
        controlParameters.wireEncode(TlvWireFormat.get()))
    generateInterest(commandInterest,
                     keyChain, certificateName,
                     TlvWireFormat.get())

    return commandInterest


def main():
    from gi.repository import GLib
    from . import base

    parser = argparse.ArgumentParser(description='Command Interest Tests')
    parser.add_argument("--faceid", "-i", type=int)
    parser.add_argument("--uri", "-u")
    parser.add_argument("--local-control-feature", "-l")
    parser.add_argument("--origin", "-o", type=int)
    parser.add_argument("--cost", "-c", type=int)
    parser.add_argument("--forwarding-flags", "-F", type=int)
    parser.add_argument("--strategy", "-s")
    parser.add_argument("--expiration-period", "-e", type=int)
    parser.add_argument("command")

    args = utils.parse_args(parser=parser)

    controlParameters = ControlParameters()

    name = None
    if args.name:
        name = Name(args.name)
    if args.faceid:
        controlParameters.setFaceId(args.faceid)
    if args.uri:
        controlParameters.setUri(args.uri)
    if args.local_control_feature:
        controlParameters.setLocalControlFeature(args.local_control_feature)
    if args.origin:
        controlParameters.setOrigin(args.origin)
    if args.cost:
        controlParameters.setCost(args.cost)
    if args.forwarding_flags:
        controlParameters.setForwardingFlags(args.forwarding_flags)
    if args.strategy:
        controlParameters.setStrategy(Name(args.strategy))
    if args.expiration_period:
        controlParameters.setExpirationPeriod(args.expiration_period)

    loop = GLib.MainLoop()

    def print_and_quit(*args, **kwargs):
        logger.info(*args, **kwargs)
        loop.quit()

    logger.info('running command: %s on %s', args.command, name)
    ndn = base.Base(name)
    ndn.expressCommandInterest(
        args.command, name, controlParameters=controlParameters,
                               interestLifeTime=10000,
                               onFailed=lambda *
                                   a: print_and_quit('FAILED: %s', a),
                               onSuccess=lambda *a: print_and_quit('SUCCESS: %s', a))
    loop.run()

if __name__ == '__main__':
    main()
