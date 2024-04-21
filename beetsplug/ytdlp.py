from beets import config
import dataclasses
from beets import ui
from beets.plugins import BeetsPlugin
import optparse
from ytmusicapi import YTMusic
from yt_dlp import YoutubeDL
import os
import subprocess

class Colors():
    INFO = '\033[94m'
    SUCCESS = '\033[92m'
    WARNING = '\033[93m'
    BOLD = '\033[1m'
    END = '\033[0m'

@dataclasses.dataclass
class AlbumDetails:
    title: str
    artist: str
    playlist_url: str

    def __str__(self):
        return f'{self.title} by {self.artist}'

class YTDLPPlugin(BeetsPlugin):
    """A plugin for downloading music from YouTube and importing into beets."""

    config: dict
    config_dir: str
    cache_dir: str

    def __init__(self, *args, **kwargs):
        """Set default values."""

        super(YTDLPPlugin, self).__init__()

        self.config_dir = config.config_dir()
        self.cache_dir = self.config_dir + "/yt_dlp"

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

        def ytdlp_func(lib, opts: optparse.Values, args: list[str]):
            """Download albums from YouTube and import into beets."""
            if self.config.get("verbose"):
                print(f"[ytdlp] Running ytdlp with opts: {opts}")

            album_details = self._get_album_details(opts.artist, opts.album, opts.url if opts.url else None)
            if not album_details:
                return

            album_dir = self._download_album(album_details)
            if not album_dir:
                return

            self._import_album(album_dir)

            print(f"{Colors.SUCCESS}[ytdlp] Successfully imported {album_details}{Colors.END}")

        ytdlp_command = ui.Subcommand(
            'ytdlp',
            help='Download albums from YouTube and import into beets',
            parser=self._parser(),
        )
        ytdlp_command.func = ytdlp_func

        return [ytdlp_command]

    def _parser(self) -> optparse.OptionParser:
        """Defines the parser for the ytdlp subcommand."""
        parser = optparse.OptionParser()
        parser.add_option(
            "--artist",
            action="store",
            help="Name of artist",
        )
        parser.add_option(
            "--album",
            action="store",
            help="Name of album",
        )
        parser.add_option(
            "-u", "--url",
            dest="url",
            action="store",
            help="URL of YouTube playlist to download. Bypasses search.",
        )
        parser.add_option(
            "-v", "--verbose",
            action="store_true",
            dest="verbose",
            default=False,
        )

        return parser 

    def _get_album_details(self, artist: str, album: str, url: str | None) -> AlbumDetails | None:
        """Get details for album playlist on YouTube."""
        # If the url has been passed in, use that
        if url:
            return AlbumDetails(title=album, artist=artist, playlist_url=url)

        # Otherwise perform a search via YTMusic api
        if self.config.get('verbose'):
            print(f'[ytdlp] Searching for {artist} - {album} on YouTube Music')
        ytmusic = YTMusic()
        search_results: list[dict] = ytmusic.search(f'{artist} {album}', filter="albums")
        if not search_results:
            print(f'[ytdlp] No results found for {artist} - {album}')
            print('[ytdlp] Please check the artist and album names and try again.')
            print('[ytdlp] Or consider passing in the url to a playlist via the --url flag.')
            return None
        album_details: dict = ytmusic.get_album(search_results[0]['browseId'])
        available_tracks: bool = all([track['available'] for track in album_details['tracks']])
        if not available_tracks:
            print(f'[ytdlp] Not all tracks are available for {album} by {artist}')
            print("[ytdlp] Consider passing in the url to a playlist via the --url flag.")
            return None
        return AlbumDetails(
            title=album_details['title'],
            artist=album_details['artists'][0]['name'],
            playlist_url="https://youtube.com/playlist?list=" + album_details['audioPlaylistId']
        )

    def _download_album(self, ad: AlbumDetails) -> str:
        """Download album from YouTube."""
        if self.config.get('verbose'):
            print(f'[ytdlp] Downloading {ad}')

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': self.cache_dir + "/" + ad.artist + "/" + ad.title + "/%(id)s.%(ext)s"
        }

        with YoutubeDL(ydl_opts) as ydl:
            returncode = ydl.download([ad.playlist_url])

        if returncode != 0:
            print(f'[ytdlp] Error downloading {ad}')
            return ""

        return self.cache_dir + "/" + ad.artist + "/" + ad.title

    def _import_album(self, album_dir: str):
        """Import album into beets."""
        beet_cmd = ['beet', 'import', '-m', album_dir]
        if os.getenv('BEETS_ENV') == 'develop':
            beet_cmd.extend(['-c', 'env.config.yml'])
        if self.config.get('verbose'):
            print("[ytdlp] Running beets: " + ' '.join(beet_cmd))
            beet_cmd.extend(['-v'])
        subprocess.run(beet_cmd)


