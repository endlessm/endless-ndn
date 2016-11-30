# -*- Mode:python; coding: utf-8; c-file-style:"gnu"; indent-tabs-mode:nil -*- */
#
# Copyright (C) 2014-2016 Regents of the University of California.
# Author: Jeff Thompson <jefft0@remap.ucla.edu>
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

import time
from pyndn import Name

import Chunks
from NDN import Endless

from gi.repository import GLib

def dump(*list):
    result = ""
    for element in list:
        result += (element if type(element) is str else str(element)) + " "
    print(result)

if __name__ == "__main__":
    import sys
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("filename")
    parser.add_argument("-n", "--name")
    parser.add_argument("-l", "--limit", type=int, default=100)
    parser.add_argument("-c", "--chunksize", type=int, default=4096)
    args = parser.parse_args()

    if not args.name:
        args.name = Name(Endless.NAMES.BASE).append("testchunks/").append(args.filename)
    args.name = Name (args.name).append ('chunked')

    producer = Chunks.Producer(args.name, args.filename, args.chunksize, auto=True)
    loop = GLib.MainLoop()

    def check(o, f):
        if args.limit and producer._responseCount > args.limit:
            loop.quit()

    producer.connect('interest', check)
    loop.run()
