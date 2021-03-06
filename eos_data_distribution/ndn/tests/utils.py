#!/usr/bin/python
# -*- Mode:python; coding: utf-8; c-file-style:"gnu"; indent-tabs-mode:nil -*- */
#
# Copyright (C) 2016 Endless Computers INC.
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

from gi.repository import GLib


def run_producer_test(producer, name, args):
    loop = GLib.MainLoop()

    producer.start()
    if args.output:
        from ..file import FileConsumer
        consumer = FileConsumer(name, filename=args.output)
        consumer.connect('complete', lambda *a: loop.quit())
        consumer.start()
    loop.run()

def run_producers_test(producers, names, args):
    loop = GLib.MainLoop()

    [producer.start() for producer in producers]
    producer.start()


    if args.output:
        from ..file import FileConsumer
        consumers = [FileConsumer(n, filename="%s-%s"%(args.output, o)) for o,n in enumerate(names)]

        def check_complete(*a):
            if all([c._emitted_complete for c in consumers]):
                print("ALL RETRIEVED")
                loop.quit()

        [consumer.connect('complete', check_complete) for consumer in consumers]
        [consumer.start() for consumer in consumers]
    loop.run()
