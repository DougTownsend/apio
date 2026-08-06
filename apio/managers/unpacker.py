"""DOC: TODO"""

# -*- coding: utf-8 -*-
# -- This file is part of the Apio project
# -- (C) 2016-2019 FPGAwars
# -- Author Jesús Arroyo
# -- License GPLv2
# -- Derived from:
# ---- Platformio project
# ---- (C) 2014-2016 Ivan Kravets <me@ikravets.com>
# ---- License Apache v2

from pathlib import Path
from os import chmod
from tarfile import open as tarfile_open
from zipfile import ZipFile
from rich.progress import track
from apio.common.apio_console import console, cerror
from apio.utils import util


class ArchiveBase:
    """DOC: TODO"""

    def __init__(self, arhfileobj, is_tar_file: bool):
        self._afo = arhfileobj
        self._is_tar_file = is_tar_file

    def get_items(self):  # pragma: no cover
        """DOC: TODO"""

        raise NotImplementedError()

    def extract_item(self, item, dest_dir):
        """DOC: TODO"""

        if hasattr(item, "filename") and item.filename.endswith(".gitignore"):
            return
        if self._is_tar_file and util.get_python_ver_tuple() >= (3, 12, 0):
            # -- Special case for avoiding the tar deprecation warning. Search
            # -- 'extraction_filter' in the page
            # -- https://docs.python.org/3/library/tarfile.html
            self._afo.extract(item, dest_dir, filter="fully_trusted")
        else:
            self._afo.extract(item, dest_dir)
        self.after_extract(item, dest_dir)

    def after_extract(self, item, dest_dir):
        """DOC: TODO"""


class TARArchive(ArchiveBase):
    """DOC: TODO"""

    def __init__(self, archpath):
        # R1732: Consider using 'with' for resource-allocating operations
        # (consider-using-with)
        # pylint: disable=R1732
        ArchiveBase.__init__(self, tarfile_open(archpath), is_tar_file=True)

    def get_items(self):
        return self._afo.getmembers()


class ZIPArchive(ArchiveBase):
    """Zip unpacker. Needed for packages whose upstream release (e.g.
    Kitware's CMake, the xPack arm-none-eabi-gcc toolchain) only ships a
    Windows build as .zip, with no .tgz alternative -- unlike apio's own
    mirrored packages, which are all repackaged as .tgz."""

    def __init__(self, archpath):
        # R1732: Consider using 'with' for resource-allocating operations
        # (consider-using-with)
        # pylint: disable=R1732
        ArchiveBase.__init__(self, ZipFile(archpath), is_tar_file=False)

    @staticmethod
    def preserve_permissions(item, dest_dir):
        """Zip doesn't restore unix executable bits on extraction the way
        tar does, so do it explicitly from the entry's stored attrs."""

        # -- Build the filename
        file = str(Path(dest_dir) / item.filename)

        attrs = item.external_attr >> 16
        if attrs:
            chmod(file, attrs)

    def get_items(self):
        return self._afo.infolist()

    def after_extract(self, item, dest_dir):
        self.preserve_permissions(item, dest_dir)


class FileUnpacker:
    """Class for unpacking compressed files"""

    def __init__(self, archpath: Path, dest_dir=Path(".")):
        """Initialize the unpacker object
        * INPUT:
          - archpath: filename with path to uncompress
          - des_dir: Destination folder
        """

        self._archpath = archpath
        self._dest_dir = dest_dir
        self._unpacker = None

        # -- Get the file extension. Path.suffix only returns the last
        # -- dot-component (".gz" for "foo.tar.gz"), so check the full
        # -- name for the two-part ".tar.gz" case too -- apio's own
        # -- mirrored packages are always single-suffix ".tgz", but
        # -- packages that reference upstream projects' own releases
        # -- directly (e.g. the pico toolchain packages) commonly use
        # -- ".tar.gz".
        arch_name = archpath.name
        arch_ext = archpath.suffix

        # -- Select the unpacker... according to the file extension
        # -- tar zip file
        if arch_ext == ".tgz" or arch_name.endswith(".tar.gz"):
            self._unpacker = TARArchive(archpath)

        # -- Zip file
        elif arch_ext == ".zip":
            self._unpacker = ZIPArchive(archpath)

        # -- Fatal error. Unknown extension.
        if not self._unpacker:
            cerror(f"Can not unpack file '{archpath}'")
            raise util.ApioException()

    def start(self) -> bool:
        """Start unpacking the file"""

        # -- Build an array with all the files inside the tarball
        if self._unpacker is not None:
            items = self._unpacker.get_items()
        else:
            items = []

        # -- Unpack while displaying a progress bar.
        for i in track(
            range(len(items)),
            description="Unpacking  ",
            console=console(),
        ):
            if self._unpacker is not None:
                self._unpacker.extract_item(items[i], self._dest_dir)

        return True
