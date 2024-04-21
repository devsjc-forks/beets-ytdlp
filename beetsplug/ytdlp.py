from beets import config
from beets import ui
from beets.plugins import BeetsPlugin
from optparse import OptionParser
from pathlib import Path
from shutil import copyfile
from xdg import BaseDirectory
from ytmusicapi import YTMusic
from yt_dlp import YoutubeDL
from hashlib import md5
import glob
import json
import os
import re
import shutil
import subprocess
import uuid

class Colors():
    INFO = '\033[94m'
    SUCCESS = '\033[92m'
    WARNING = '\033[93m'
    BOLD = '\033[1m'
    END = '\033[0m'

class YTDLPPlugin(BeetsPlugin):
    """A plugin for downloading music from YouTube and importing into beets."""

    def __init__(self, *args, **kwargs):
        """Set default values."""

        super(YdlPlugin, self).__init__()

        self.playlist_url = "https://www.youtube.com/playlist?list="
        self.config_dir = config.config_dir()
        self.cache_dir = self.config_dir + "/yt_dlp"
        self.outtmpl = self.cache_dir + "/%(id)s/%(id)s.%(ext)s"

        # Default options
        self._config = {
            'verbose': False,
        }
        self._config.update(self.config)
        self.config = self._config

        # be verbose if beets is verbose
        if not self.config.get('verbose'):
            self.config['verbose'] = True

    def commands(self):
        """Add commands to beets CLI."""


        return [ydl_cmd]

    def _command_ytdlp() -> ui.Subcommand:
        """Defines the entrypoint for the ytdlp command."""
        parser = OptionParser()
        parser.add_option(
            'artist',
            help="Name of artist of album",
        )
        parser.add_option(
            'album',
            help="Name of album to search for",
        )
        parser.add_option(
            "-v", "--verbose",
            dest="verbose",
            default=False,
        )

        ydl_cmd = ui.Subcommand(
            'ytdlp',
            parser=parser,
            help="Download album from YouTube and import into beets",
        )
        ydl_cmd.func = ydl_func

        return ydl_cmd
    
    def search_album(self, artist, album) -> dict:
        """Search for album on YouTube."""
        ytmusic = YTMusic()
        search_results: list[dict] = ytmusic.search(f'{artist} {album}', filter="albums")
        if not search_results:
            print(f'No results found for {artist} - {album}')
            return {}
        album_details: dict = ytmusic.get_album(search_results[0]['browseId'])
        return album_details

    def download_album(self, album_details: dict) -> None:
        """Download album from YouTube."""
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': self.outtmpl,
        }
        album_title: str = album_details['title']
        album_artist: str = album_details['artists'][0]["name"]
        playlist_url: str = self.playlist_url + album_details['audioPlaylistId']
        available_tracks: bool = all([track['available'] for track in album_details['tracks']])
        if not available_tracks:
            print(f'Not all tracks are available for {album_title} by {album_artist}')
            return
        # Make album/artist folder in cache directory
        album_dir: Path = Path(self.cache_dir) / album_artist / album_title
        album_dir.mkdir(parents=True, exist_ok=True)

        with YoutubeDL() as ydl:
            ydl.download([playlist_url])

    def youtubedl(self, lib, opts, arg):
        """Calls YoutubeDL

        Call beets when finishes downloading the audio file. We don't implement
        a YoutubeDL's post processor because we want to call beets for every
        download, and not after downloading a lot of files.

        So we try to read `YoutubeDL.extract_info` entries and process them
        with an internal `YoutubeDL.process_ie_result` method, that will
        actually download the audio file.
        """
        if self.config.get('verbose'):
            print("[ydl] Calling youtube-dl")

        youtubedl_config = self.config.get('youtubedl_options')
        youtubedl_config['keepvideo'] = self.config.get('keep_files')
        y = YoutubeDL(youtubedl_config)

        ie_result = y.extract_info(arg, download=False, process=False)

        if ie_result is None:
            print("[ydl] Error: Failed to fetch file information.")
            print("[ydl]   If this is not a network problem, try upgrading")
            print("[ydl]   beets-ydl:")
            print("[ydl]")
            print("[ydl]     pip install -U beets-ydl")
            print("[ydl]")
            exit(1)

        if 'entries' in ie_result:
            entries = ie_result['entries']
        else:
            entries = [ie_result]

        download = self.config.get('download')
        if self.config.get('force_download'):
            download = True

        for entry in entries:
            items = [x for x in lib.items('ydl:' + entry['id'])] + \
                [x for x in lib.albums('ydl:' + entry['id'])]

            if len(items) > 0 and not self.config.get('force_download'):
                if self.config.get('verbose'):
                    print('[ydl] Skipping item already in library:' + \
                        ' %s [%s]' % (entry['title'], entry['id']))
                continue

            if self.config.get('verbose') and not download:
                print("[ydl] Skipping download: " + entry['id'])

            data = y.process_ie_result(entry, download=download)
            if data:
                ie_result.update(data)
                self.info = ie_result
                self.process_item()
            else:
                print("[ydl] No data for " + entry['id'])

    def is_in_library(self, entry, lib):
        """Check if an `entry` is already in the `lib` beets library
        """
        if lib.items(('ydl_id', entry['id'])):
            return True
        else:
            return False

    def process_item(self):
        """Called after downloading source with YoutubeDL

        From here on, the plugin assumes its state according to what
        is being downloaded.
        """
        print('[ydl] Processing item: ' + self.info.get('title'))

        ext = self.config.get('youtubedl_options')\
                ['postprocessors'][0]['preferredcodec']
        self.audio_file = self.get_file_path(ext)
        self.outdir, self.audio_file_ext = os.path.splitext(self.audio_file)
        self.outdir = os.path.dirname(self.outdir)

        if self.config.get('verbose') and \
            self.config.get('download') and \
            not os.path.exists(self.audio_file):
            print('[ydl] Error: Audio file not found: ' + self.audio_file)
            exit(1)

        self.strip_fullalbum()
        self.extract_tracks()

        if not self.is_album():
            self.set_single_file_data()

        if self.config.get('verbose'):
            print(self.get_tracklist())

        if self.config.get('write_dummy_mp3'):
            self.write_dummy_mp3()

        if self.config.get('verbose') and self.is_album():
            print("[ydl] URL is identified as an album")
        else:
            print("[ydl] URL is identified as a singleton")

        if self.config.get('split_files') \
            and not self.config.get('write_dummy_mp3') \
            and self.is_album():
            self.split_file()

        if self.config.get('import'):
            beet_cmd = self.get_beet_cmd()
            if self.config.get('verbose'):
                print("[ydl] Running beets: " + ' '.join(beet_cmd))
            subprocess.run(beet_cmd)
        elif self.config.get('verbose'):
            print('[ydl] Skipping import')

        if not self.config.get('keep_files'):
            self.clean()
        elif self.config.get('verbose') and self.config.get('keep_files'):
            print('[ydl] Keeping downloaded files on ' + self.outdir)

    def get_beet_cmd(self):
        beet_cmd = ['beet']

        if os.getenv('BEETS_ENV') == 'develop':
            beet_cmd.extend(['-c', 'env.config.yml'])

        if self.config.get('verbose'):
            beet_cmd.extend(['-v'])

        beet_cmd.extend(['import', '--set', 'ydl=' + self.info.get('id')])

        if not self.is_album():
            beet_cmd.extend(['--singletons'])

        if os.path.exists(self.outdir):
            beet_cmd.extend([self.outdir])
        else:
            beet_cmd.extend([self.audio_file])

        return beet_cmd

    def __exit__(self, exc_type, exc_value, traceback):
        cache_size = self.config.get('cache_dir')
        if cache_size > 0:
            print("[ydl] " + cache_size + " in cache")

        if self.config.get('verbose'):
            print('[ydl] Leaving')

    def clean(self):
        """Deletes everything related to the present run.
        """
        files = glob.glob(self.outdir + '*')
        for f in files:
            if os.path.isdir(f):
                shutil.rmtree(f)
            else:
                os.remove(f)


