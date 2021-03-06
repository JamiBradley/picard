# -*- coding: utf-8 -*-
#
# Picard, the next-generation MusicBrainz tagger
# Copyright (C) 2007 Oliver Charles
# Copyright (C) 2007-2011 Philipp Wolfer
# Copyright (C) 2007, 2010, 2011 Lukáš Lalinský
# Copyright (C) 2011 Michael Wiencek
# Copyright (C) 2011-2012 Wieland Hoffmann
# Copyright (C) 2013-2014 Laurent Monin
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
import os
import shutil
import sys
import tempfile

from hashlib import md5
from PyQt4.QtCore import QUrl, QObject, QMutex
from picard import config, log
from picard.coverart.utils import translate_caa_type
from picard.script import ScriptParser
from picard.util import (
    encode_filename,
    replace_win32_incompat,
    imageinfo
)
from picard.util.textencoding import (
    replace_non_ascii,
    unaccent,
)


_datafiles = dict()
_datafile_mutex = QMutex(QMutex.Recursive)


class DataHash:

    def __init__(self, data, prefix='picard', suffix=''):
        self._filename = None
        _datafile_mutex.lock()
        try:
            m = md5()
            m.update(data)
            self._hash = m.hexdigest()
            if self._hash not in _datafiles:
                (fd, self._filename) = tempfile.mkstemp(prefix=prefix, suffix=suffix)
                QObject.tagger.register_cleanup(self.delete_file)
                with os.fdopen(fd, "wb") as imagefile:
                    imagefile.write(data)
                _datafiles[self._hash] = self._filename
                log.debug("Saving image data %s to %r" % (self._hash, self._filename))
            else:
                self._filename = _datafiles[self._hash]
        finally:
            _datafile_mutex.unlock()

    def delete_file(self):
        if self._filename:
            try:
                os.unlink(self._filename)
            except:
                pass
            else:
                _datafile_mutex.lock()
                try:
                    self._filename = None
                    del _datafiles[self._hash]
                    self._hash = None
                finally:
                    _datafile_mutex.unlock()

    @property
    def data(self):
        if self._filename:
            with open(self._filename, "rb") as imagefile:
                return imagefile.read()
        return None

    @property
    def filename(self):
        return self._filename


class CoverArtImageError(Exception):
    pass


class CoverArtImageIOError(CoverArtImageError):
    pass


class CoverArtImageIdentificationError(CoverArtImageError):
    pass


class CoverArtImage:

    # Indicate if types are provided by the source, ie. CAA or certain file
    # formats may have types associated with cover art, but some other sources
    # don't provide such information
    support_types = False
    # `is_front` has to be explicitely set, it is used to handle CAA is_front
    # indicator
    is_front = None
    sourceprefix = "URL"

    def __init__(self, url=None, types=[], comment='', data=None):
        if url is not None:
            self.parse_url(url)
        else:
            self.url = None
        self.types = types
        self.comment = comment
        self.datahash = None
        # thumbnail is used to link to another CoverArtImage, ie. for PDFs
        self.thumbnail = None
        self.can_be_saved_to_tags = True
        self.can_be_saved_to_disk = True
        self.can_be_saved_to_metadata = True
        if data is not None:
            self.set_data(data)

    def parse_url(self, url):
        self.url = QUrl(url)
        self.host = str(self.url.host())
        self.port = self.url.port(80)
        self.path = str(self.url.encodedPath())
        if self.url.hasQuery():
            self.path += '?' + str(self.url.encodedQuery())

    @property
    def source(self):
        if self.url is not None:
            return u"%s: %s" % (self.sourceprefix, self.url.toString())
        else:
            return u"%s" % self.sourceprefix

    def is_front_image(self):
        """Indicates if image is considered as a 'front' image.
        It depends on few things:
            - if `is_front` was set, it is used over anything else
            - if `types` was set, search for 'front' in it
            - if `support_types` is False, default to True for any image
            - if `support_types` is True, default to False for any image
        """
        if not self.can_be_saved_to_metadata:
            # ignore thumbnails
            return False
        if self.is_front is not None:
            return self.is_front
        if u'front' in self.types:
            return True
        return (self.support_types is False)

    def imageinfo_as_string(self):
        if self.datahash is None:
            return ""
        return "w=%d h=%d mime=%s ext=%s datalen=%d file=%s" % (self.width,
                                                                self.height,
                                                                self.mimetype,
                                                                self.extension,
                                                                self.datalength,
                                                                self.tempfile_filename)

    def __repr__(self):
        p = []
        if self.url is not None:
            p.append("url=%r" % self.url.toString())
        if self.types:
            p.append("types=%r" % self.types)
        if self.is_front is not None:
            p.append("is_front=%r" % self.is_front)
        if self.comment:
            p.append("comment=%r" % self.comment)
        return "%s(%s)" % (self.__class__.__name__, ", ".join(p))

    def __unicode__(self):
        p = [u'Image']
        if self.url is not None:
            p.append(u"from %s" % self.url.toString())
        if self.types:
            p.append(u"of type %s" % u','.join(self.types))
        if self.comment:
            p.append(u"and comment '%s'" % self.comment)
        return u' '.join(p)

    def __str__(self):
        return unicode(self).encode('utf-8')

    def set_data(self, data):
        """Store image data in a file, if data already exists in such file
           it will be re-used and no file write occurs
        """
        if self.datahash:
            self.datahash.delete_file()
            self.datahash = None

        try:
            (self.width, self.height, self.mimetype, self.extension,
             self.datalength) = imageinfo.identify(data)
        except imageinfo.IdentificationError as e:
            raise CoverArtImageIdentificationError(e)

        try:
            self.datahash = DataHash(data, suffix=self.extension)
        except (OSError, IOError) as e:
            raise CoverArtImageIOError(e)

    @property
    def maintype(self):
        """Returns one type only, even for images having more than one type set.
        This is mostly used when saving cover art to tags because most formats
        don't support multiple types for one image.
        Images coming from CAA can have multiple types (ie. 'front, booklet').
        """
        if self.is_front_image() or not self.types or u'front' in self.types:
            return u'front'
        # TODO: do something better than randomly using the first in the list
        return self.types[0]

    def _make_image_filename(self, filename, dirname, metadata):
        filename = ScriptParser().eval(filename, metadata)
        if config.setting["ascii_filenames"]:
            if isinstance(filename, unicode):
                filename = unaccent(filename)
            filename = replace_non_ascii(filename)
        if not filename:
            filename = "cover"
        if not os.path.isabs(filename):
            filename = os.path.join(dirname, filename)
        # replace incompatible characters
        if config.setting["windows_compatibility"] or sys.platform == "win32":
            filename = replace_win32_incompat(filename)
        # remove null characters
        filename = filename.replace("\x00", "")
        return encode_filename(filename)

    def save(self, dirname, metadata, counters):
        """Saves this image.

        :dirname: The name of the directory that contains the audio file
        :metadata: A metadata object
        :counters: A dictionary mapping filenames to the amount of how many
                    images with that filename were already saved in `dirname`.
        """
        if not self.can_be_saved_to_disk:
            return
        if (config.setting["caa_image_type_as_filename"] and
            not self.is_front_image()):
            filename = self.maintype
            log.debug("Make cover filename from types: %r -> %r",
                      self.types, filename)
        else:
            filename = config.setting["cover_image_filename"]
            log.debug("Using default cover image filename %r", filename)
        filename = self._make_image_filename(filename, dirname, metadata)

        overwrite = config.setting["save_images_overwrite"]
        ext = self.extension
        image_filename = self._next_filename(filename, counters)
        while os.path.exists(image_filename + ext) and not overwrite:
            if not self._is_write_needed(image_filename + ext):
                break
            image_filename = self._next_filename(filename, counters)
        else:
            new_filename = image_filename + ext
            # Even if overwrite is enabled we don't need to write the same
            # image multiple times
            if not self._is_write_needed(new_filename):
                return
            log.debug("Saving cover image to %r", new_filename)
            try:
                new_dirname = os.path.dirname(new_filename)
                if not os.path.isdir(new_dirname):
                    os.makedirs(new_dirname)
                shutil.copyfile(self.tempfile_filename, new_filename)
            except (OSError, IOError) as e:
                raise CoverArtImageIOError(e)

    def _next_filename(self, filename, counters):
        if counters[filename]:
            new_filename = "%s (%d)" % (filename, counters[filename])
        else:
            new_filename = filename
        counters[filename] += 1
        return new_filename

    def _is_write_needed(self, filename):
        if (os.path.exists(filename)
                and os.path.getsize(filename) == self.datalength):
            log.debug("Identical file size, not saving %r", filename)
            return False
        return True

    @property
    def data(self):
        """Reads the data from the temporary file created for this image.
        May raise CoverArtImageIOError
        """
        try:
            return self.datahash.data
        except (OSError, IOError) as e:
            raise CoverArtImageIOError(e)

    @property
    def tempfile_filename(self):
        return self.datahash.filename

    def types_as_string(self, translate=True, separator=', '):
        if self.types:
            types = self.types
        elif self.is_front_image():
            types = [u'front']
        else:
            types = [u'-']
        if translate:
            types = [translate_caa_type(type) for type in types]
        return separator.join(types)


class CaaCoverArtImage(CoverArtImage):

    """Image from Cover Art Archive"""

    support_types = True
    sourceprefix = u"CAA"

    def __init__(self, url, types=[], is_front=False, comment='', data=None):
        CoverArtImage.__init__(self, url=url, types=types, comment=comment,
                               data=data)
        self.is_front = is_front


class CaaThumbnailCoverArtImage(CaaCoverArtImage):

    """Used for thumbnails of CaaCoverArtImage objects, together with thumbnail
    property"""

    def __init__(self, url, types=[], is_front=False, comment='', data=None):
        CaaCoverArtImage.__init__(self, url=url, types=types, comment=comment,
                                  data=data)
        self.is_front = False
        self.can_be_saved_to_disk = False
        self.can_be_saved_to_tags = False
        self.can_be_saved_to_metadata = False


class TagCoverArtImage(CoverArtImage):

    """Image from file tags"""

    def __init__(self, file, tag=None, types=[], is_front=None,
                 support_types=False, comment='', data=None):
        CoverArtImage.__init__(self, url=None, types=types, comment=comment,
                               data=data)
        self.sourcefile = file
        self.tag = tag
        self.support_types = support_types
        if is_front is not None:
            self.is_front = is_front

    @property
    def source(self):
        if self.tag:
            return u'Tag %s from %s' % (self.tag, self.sourcefile)
        else:
            return u'File %s' % (self.sourcefile)

    def __repr__(self):
        p = []
        p.append('%r' % self.sourcefile)
        if self.tag is not None:
            p.append("tag=%r" % self.tag)
        if self.types:
            p.append("types=%r" % self.types)
        if self.is_front is not None:
            p.append("is_front=%r" % self.is_front)
        p.append('support_types=%r' % self.support_types)
        if self.comment:
            p.append("comment=%r" % self.comment)
        return "%s(%s)" % (self.__class__.__name__, ", ".join(p))


class CoverArtImageFromFile(CoverArtImage):

    sourceprefix = 'LOCAL'

    def __init__(self, filepath, types=[], is_front=None,
                 support_types=False, comment='', data=None):
        CoverArtImage.__init__(self, url=None, types=types, comment=comment,
                               data=data)
        self.filepath = filepath
        self.support_types = support_types
        if is_front is not None:
            self.is_front = is_front

    @property
    def source(self):
        return u'%s %s' % (self.sourceprefix, self.filepath)

    def __repr__(self):
        p = []
        p.append('%r' % self.filepath)
        if self.types:
            p.append("types=%r" % self.types)
        if self.is_front is not None:
            p.append("is_front=%r" % self.is_front)
        p.append('support_types=%r' % self.support_types)
        if self.comment:
            p.append("comment=%r" % self.comment)
        return "%s(%s)" % (self.__class__.__name__, ", ".join(p))
