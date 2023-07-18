from __future__ import unicode_literals

import hashlib
import json
import os
import platform
import subprocess
import sys
import traceback
from zipimport import zipimporter

from .compat import compat_realpath
from .utils import encode_compat_str

from .version import __version__

REPO = 'ytdl-org/ytdl-nightly'


def rsa_verify(message, signature, key):
    assert isinstance(message, bytes)
    byte_size = (len(bin(key[0])) - 2 + 8 - 1) // 8
    signature = ('%x' % pow(int(signature, 16), key[1], key[0])).encode()
    signature = (byte_size * 2 - len(signature)) * b'0' + signature
    asn1 = b'3031300d060960864801650304020105000420'
    asn1 += hashlib.sha256(message).hexdigest().encode()
    if byte_size < len(asn1) // 2 + 11:
        return False
    expected = b'0001' + (byte_size - len(asn1) // 2 - 3) * b'ff' + b'00' + asn1
    return expected == signature


def detect_variant():
    if hasattr(sys, 'frozen'):
        if getattr(sys, '_MEIPASS', None):
            return 'win_exe'
        return 'py2exe'
    elif isinstance(globals().get('__loader__'), zipimporter):
        return 'zip'
    elif os.path.basename(sys.argv[0]) == '__main__.py':
        return 'source'
    return 'unknown'


_NON_UPDATEABLE_REASONS = {
    'win_exe': None,
    'zip': None,
    'mac_exe': None,
    'py2exe': None,
    'win_dir': 'Auto-update is not supported for unpackaged windows executable; Re-download the latest release',
    'mac_dir': 'Auto-update is not supported for unpackaged MacOS executable; Re-download the latest release',
    'source': 'You cannot update when running from source code; Use git to pull the latest changes',
    'unknown': 'It looks like you installed youtube-dl with a package manager, pip or setup.py; Use that to update',
}


def is_non_updateable():
    return _NON_UPDATEABLE_REASONS.get(detect_variant(), _NON_UPDATEABLE_REASONS['unknown'])


def run_update(ydl):
    """
    Update the program file with the latest version from the repository
    Returns whether the program should terminate
    """

    JSON_URL = 'https://api.github.com/repos/%s/releases/latest' % (REPO, )

    def report_error(msg, expected=False):
        ydl.report_error(msg, tb='' if expected else None)

    def report_unable(action, expected=False):
        report_error('Unable to %s' % action, expected)

    def report_permission_error(file):
        report_unable('write to %s; Try running as administrator' % file, True)

    def report_network_error(action, delim=';'):
        report_unable('%s%s Visit  https://github.com/%s/releases/latest' % (action, delim, REPO), True)

    def calc_sha256sum(path):
        h = hashlib.sha256()
        b = bytearray(128 * 1024)
        mv = memoryview(b)
        with open(os.path.realpath(path), 'rb', buffering=0) as f:
            for n in iter(lambda: f.readinto(mv), 0):
                h.update(mv[:n])
        return h.hexdigest()

    # Download and check versions info
    try:
        version_info = ydl._opener.open(JSON_URL).read().decode('utf-8')
        version_info = json.loads(version_info)
    except Exception:
        return report_network_error('obtain version info', delim='; Please try again later or')

    def version_tuple(version_str):
        return tuple(map(int, version_str.split('.')))

    version_id = version_info['tag_name']
    ydl.to_screen('Latest version: %s, Current version: %s' % (version_id, __version__))
    if version_tuple(__version__) >= version_tuple(version_id):
        ydl.to_screen('youtube-dl is up to date (%s)' % __version__)
        return

    err = is_non_updateable()
    if err:
        return report_error(err, True)

    # sys.executable is set to the full pathname of the exe-file for py2exe
    # though symlinks are not followed so that we need to do this manually
    # with help of realpath
    filename = compat_realpath(sys.executable if hasattr(sys, 'frozen') else sys.argv[0])
    ydl.to_screen('Current Build Hash %s' % calc_sha256sum(filename))
    ydl.to_screen('Updating to version %s ...' % version_id)

    version_labels = {
        'zip_3': '',
        'py2exe_32': '.exe',
        'py2exe_64': '.exe',
    }

    def get_bin_info(bin_or_exe, version):
        label = version_labels['%s_%s' % (bin_or_exe, version)]
        return next((i for i in version_info['assets'] if i['name'] == 'youtube-dl%s' % label), {})

    def get_sha256sum(bin_or_exe, version):
        filename = 'youtube-dl%s' % version_labels['%s_%s' % (bin_or_exe, version)]
        urlh = next(
            (i for i in version_info['assets'] if i['name'] in ('SHA2-256SUMS')),
            {}).get('browser_download_url')
        if not urlh:
            return None
        hash_data = ydl._opener.open(urlh).read().decode('utf-8')
        return dict(ln.split()[::-1] for ln in hash_data.splitlines()).get(filename)

    if not os.access(filename, os.W_OK):
        return report_permission_error(filename)

    # PyInstaller
    variant = detect_variant()
    if variant in ('win_exe', 'py2exe'):
        directory = os.path.dirname(filename)
        if not os.access(directory, os.W_OK):
            return report_permission_error(directory)
        try:
            if os.path.exists(filename + '.old'):
                os.remove(filename + '.old')
        except (IOError, OSError):
            return report_unable('remove the old version')

        try:
            arch = platform.architecture()[0][:2]
            url = get_bin_info(variant, arch).get('browser_download_url')
            if not url:
                return report_network_error('fetch updates')
            urlh = ydl._opener.open(url)
            newcontent = urlh.read()
            urlh.close()
        except (IOError, OSError):
            return report_network_error('download latest version')

        try:
            with open(filename + '.new', 'wb') as outf:
                outf.write(newcontent)
        except (IOError, OSError):
            return report_permission_error('%s.new' % filename)

        expected_sum = get_sha256sum(variant, arch)
        if not expected_sum:
            ydl.report_warning('no hash information found for the release')
        elif calc_sha256sum(filename + '.new') != expected_sum:
            report_network_error('verify the new executable')
            try:
                os.remove(filename + '.new')
            except OSError:
                return report_unable('remove corrupt download')

        try:
            os.rename(filename, filename + '.old')
        except (IOError, OSError):
            return report_unable('move current version')
        try:
            os.rename(filename + '.new', filename)
        except (IOError, OSError):
            report_unable('overwrite current version')
            os.rename(filename + '.old', filename)
            return
        try:
            # Continues to run in the background
            subprocess.Popen(
                'ping 127.0.0.1 -n 5 -w 1000 & del /F "%s.old"' % filename,
                shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            ydl.to_screen('Updated youtube-dl to version %s' % version_id)
            return True  # Exit app
        except OSError:
            report_unable('delete the old version')

    elif variant in ('zip', 'mac_exe'):
        pack_type = '3' if variant == 'zip' else '64'
        try:
            url = get_bin_info(variant, pack_type).get('browser_download_url')
            if not url:
                return report_network_error('fetch updates')
            urlh = ydl._opener.open(url)
            newcontent = urlh.read()
            urlh.close()
        except (IOError, OSError):
            return report_network_error('download the latest version')

        expected_sum = get_sha256sum(variant, pack_type)
        if not expected_sum:
            ydl.report_warning('no hash information found for the release')
        elif hashlib.sha256(newcontent).hexdigest() != expected_sum:
            return report_network_error('verify the new package')

        try:
            with open(filename, 'wb') as outf:
                outf.write(newcontent)
        except (IOError, OSError):
            return report_unable('overwrite current version')

        ydl.to_screen('Updated youtube-dl to version %s; Restart youtube-dl to use the new version' % version_id)
        return

    assert False, ('Unhandled variant: %s' % variant)


# Deprecated
def update_self(to_screen, verbose, opener):

    printfn = to_screen

    class FakeYDL():
        _opener = opener
        to_screen = printfn

        @staticmethod
        def report_warning(msg, *args, **kwargs):
            return printfn('WARNING: %s' % msg, *args, **kwargs)

        @staticmethod
        def report_error(msg, tb=None):
            printfn('ERROR: %s' % msg)
            if not verbose:
                return
            if tb is None:
                # Copied from YoutubeDl.trouble
                if sys.exc_info()[0]:
                    tb = ''
                    if hasattr(sys.exc_info()[1], 'exc_info') and sys.exc_info()[1].exc_info[0]:
                        tb += ''.join(traceback.format_exception(*sys.exc_info()[1].exc_info))
                    tb += encode_compat_str(traceback.format_exc())
                else:
                    tb_data = traceback.format_list(traceback.extract_stack())
                    tb = ''.join(tb_data)
            if tb:
                printfn(tb)

    return run_update(FakeYDL())
