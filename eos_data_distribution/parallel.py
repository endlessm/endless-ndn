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

import logging

from gi.repository import GObject

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class Batch(GObject.GObject):
    __gsignals__ = {
        'complete': (GObject.SIGNAL_RUN_FIRST, None, ()),
    }

    def __init__(self, batchs, type="Batch"):
        super(Batch, self).__init__()
        self._type = type
        self._incomplete_batchs = set(batchs)
        for batch in self._incomplete_batchs:
            batch.connect('complete', self._on_batch_complete)

    def _on_batch_complete(self, batch):
        logger.info("%s complete: %s", (self._type, batch))
        self._incomplete_batchs.remove(batch)
        if len(self._incomplete_batchs) == 0:
            self.emit('complete')