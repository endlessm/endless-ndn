# -*- coding: utf-8 -*-

import errno
import gi
import logging
import os

gi.require_version('Gio', '2.0')
from gi.repository import Gio

from . import fallocate
from .dbus import chunks
from .segments import File as SegmentsFile

logger = logging.getLogger(__name__)


def get_file_size(f):
    f.seek(0, os.SEEK_END)
    return f.tell()


class FileProducer(chunks.Producer):

    def __init__(self, name, file, *args, **kwargs):
        super(FileProducer, self).__init__(name, *args, **kwargs)
        self.name = name
        self.f = file
        self._file_size = get_file_size(self.f)

    def _get_final_segment(self):
        return ((self._file_size + self.chunk_size - 1) // self.chunk_size) - 1

    def _get_chunk(self, n):
        pos = self.chunk_size * n

        if pos >= self._file_size:
            return None

        self.f.seek(pos, os.SEEK_SET)
        return self.f.read(self.chunk_size)


def mkdir_p(dirname):
    if not dirname:
        return

    try:
        os.makedirs(dirname, 0o755)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(dirname):
            pass
        else:
            raise


class Consumer(chunks.Consumer):

    def __init__(self, *args, **kwargs):
        super(Consumer, self).__init__(*args, **kwargs)

        self._part_filename = None
        self._part_fd = -1

        # If we attempt to start downloading a file in parallel with another
        # Consumer, stop downloading and monitor the other consumer's progress
        # instead.
        self._completion_monitor_file = None
        self._completion_monitor = None
        self._completion_monitor_id = -1


    def _open_files(self):
        raise NotImplementedError()

    def _save_chunk(self, n, data):
        if self._part_fd < 0:
            if not self._open_files():
                return False

        assert self._part_fd >= 0
        offs = self.chunk_size * n
        os.lseek(self._part_fd, offs, os.SEEK_SET)
        os.write(self._part_fd, data)
        self._segments_file.write(self._segments)

        return True

    def _on_complete(self, *args, **kwargs):
        os.close(self._part_fd)
        self._part_fd = -1

        os.rename(self._part_filename, self._filename)
        os.chmod(self._filename, 0o644)

        self._segments_file.close(unlink=True)
        super(Consumer, self)._on_complete(*args, **kwargs)

    def _create_files(self, filename):
        # XXX this is racy
        assert filename
        logger.debug('Opening files for ‘%s’', filename)

        mkdir_p(os.path.dirname(filename))
        self._part_filename = '%s.part' % (filename, )
        self._part_fd = os.open(
            self._part_filename, os.O_CREAT | os.O_WRONLY | os.O_NONBLOCK, 0o600)

        try:
            self._segments_file.lock()
        except IOError as e:
            # Clean up.
            os.close(self._part_fd)
            self._part_fd = -1
            self._part_filename = None

            if e.errno == errno.EAGAIN:
                # Cannot acquire lock: some other process (or part of this
                # process) is already downloading it. Clean up and watch that
                # file for completion.
                logger.debug('File ‘%s.sgt’ is locked: waiting on completion.',
                             filename)
                self._watch_for_completion(filename)
                return False
            else:
                raise

        # XXX hack
        return True

        # Reserve space for the full file and truncate any existing content to
        # the start of the final chunk (because it might be smaller than the
        # chunk size).
        size = self.chunk_size * (self._num_segments - 1)
        try:
            fallocate.fallocate(self._part_fd, 0, size)
        except IOError as e:  # if it fails, we might get surprises later, but it's ok.
            logger.debug('Error calling fallocate(%u, 0, %u): %s' %
                         (self._part_fd, self._size, e.message))
        try:
            os.ftruncate(self._part_fd, size)
        except IOError as e:
            logger.debug('Error calling ftruncate(%u, %u): %s' %
                         (self._part_fd, size, e.message))

        return True

    def _watch_for_completion(self, filename):
        assert filename
        segment_filename = '%s.sgt' % (filename, )
        logger.debug('Logging ‘%s’ for completion', segment_filename)
        monitor_file = Gio.File.new_for_path(segment_filename)
        try:
            monitor = monitor_file.monitor_file(Gio.FileMonitorFlags.NONE)
        except Exception as e:
            # TODO
            logger.info('Failed to monitor file ‘%s’: %s', segment_filename,
                        e.message)
            raise

        signal_id = monitor.connect('changed', self._on_file_monitor_changed)

        self._completion_monitor_file = monitor_file
        self._completion_monitor = monitor
        self._completion_monitor_id = signal_id

    def _on_file_monitor_changed(self, monitor, file, other_file, event_type):
        logger.debug('File monitor event: %s, %s, %u', file.get_path(),
                     other_file.get_path() if other_file else '(none)',
                     event_type)

        if (event_type == Gio.FileMonitorEvent.DELETED and
            file.equal(self._completion_monitor_file)):
            # Looks like the segment file has been deleted; try downloading
            # the file again.
            logger.info('File ‘%s’ deleted: restarting download',
                        file.get_path())

            self._completion_monitor_file = None
            self._completion_monitor.disconnect(self._completion_monitor_id)
            self._completion_monitor = None
            self._completion_monitor_id = -1

            self.start()



class FileConsumer(Consumer):

    def __init__(self, name, filename, *args, **kwargs):
        self._filename = filename
        super(FileConsumer, self).__init__(name, *args, **kwargs)

        # If we have an existing download to resume, use that. Otherwise,
        # request the first segment to bootstrap us.
        try:
            # we need to make the dir early, so that the sgt file can be created
            mkdir_p(os.path.dirname(filename))
            self._segments_file = SegmentsFile(self._filename)
            self._segments = self._segments_file.read()
        except ValueError as e:
            pass

    def _open_files(self):
        return self._create_files(self._filename)

def is_subdir(sub_dir, parent_dir):
    sub_dir = os.path.realpath(sub_dir)
    parent_dir = os.path.realpath(parent_dir)
    diff = os.path.relpath(sub_dir, parent_dir)
    return not (diff == os.pardir or diff.startswith(os.pardir + os.sep))


class DirConsumer(Consumer):

    def __init__(self, name, dirname, *args, **kwargs):
        self._dirname = dirname
        super(DirConsumer, self).__init__(name, *args, **kwargs)

    def _open_files(self):
        # os.path.join() discards preceding components if any component starts
        # with a slash.
        assert self._qualified_name
        chunkless_name = str(self._qualified_name)
        chunkless_name = chunkless_name.strip('/')

        mkdir_p(self._dirname)
        self._filename = os.path.join(self._dirname, chunkless_name)
        assert is_subdir(self._filename, self._dirname)

        return self._create_files(self._filename)


if __name__ == '__main__':
    import argparse
    from .. import utils
    from gi.repository import GLib

    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output")
    args = utils.parse_args(parser=parser)

    loop = GLib.MainLoop()


    name = args.name or args.output

    consumer = FileConsumer(name, args.output)
    consumer.connect('complete', lambda *a: loop.quit())
    consumer.start()
    loop.run()
