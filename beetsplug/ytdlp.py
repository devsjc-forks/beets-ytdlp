from beets import config
import dataclasses
from beets import ui
from beets.plugins import BeetsPlugin
import optparse
import mediafile
from ytmusicapi import YTMusic
from yt_dlp import YoutubeDL
import os

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

@dataclasses.dataclass
class SingletonDetails:
    title: str
    artist: str
    track_url: str

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
        self.cache_dir = config["directory"].get() + "/.import"

        # Default options
        self._config = {
            'verbose': False,
        }
        self._config.update(self.config)
        self.config = self._config

        # Be verbose if beets is verbose
        if not self.config.get('verbose'):
            self.config['verbose'] = True

        # Custom metadata fields
        # See
        # - https://discourse.beets.io/t/how-to-use-custom-fields/202
        # - https://beets.readthedocs.io/en/stable/dev/plugins.html#extend-mediafile
        source_url_field = mediafile.MediaField(
            mediafile.MP3DescStorageStyle(u'SourceURL'),
            mediafile.StorageStyle(u'WOAF'),
        )
        self.add_media_field(u'source_url', source_url_field)

    def commands(self):
        """Add commands to beets CLI."""

        def ytdlp_func(lib, opts: optparse.Values, args: list[str]):
            """Download albums from YouTube and import into beets."""
            if self.config.get("verbose"):
                print(f"[ytdlp] Running ytdlp with opts: {opts}")

            # Album download mode
            if opts.artist and opts.album and not opts.track:
                album_details = self._get_album_details(opts.artist, opts.album, opts.url)
                if not album_details:
                    return

                album_dir = self._download_album(album_details)
                if not album_dir:
                    return

                self._import_album(lib, album_dir)

                print(f"{Colors.SUCCESS}[ytdlp] Successfully imported {album_details}{Colors.END}")
                return

            # Track download mode
            if opts.artist and opts.track and not opts.album:
                track_details = self._get_track_details(opts.artist, opts.track, opts.url)
                if not track_details:
                    return

                track_dir = self._download_singleton(track_details)
                if not track_dir:
                    return

                self._import_singleton(lib, track_dir)

                print(f"{Colors.SUCCESS}[ytdlp] Successfully imported {track_details}{Colors.END}")
                return

            # Missing items mode
            if opts.fetch_missing:
                missing_albums: list[AlbumDetails] = self._list_missing(lib)

            else:
                print("\n".join((
                    f"{Colors.WARNING}[ytdlp] Invalid arguments. Please specify either:",
                    "\t--artist and --album",
                    "\t--artist and --track",
                    "\t--fetch-missing{Colors.END}",
                )))
                return

        ytdlp_command = ui.Subcommand(
            'ytdlp',
            help='Download albums from YouTube and import into beets',
            parser=self._parser(),
        )
        ytdlp_command.func = ytdlp_func

        return [ytdlp_command]

    @staticmethod
    def _parser() -> optparse.OptionParser:
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
            "--track",
            action="store",
            help="Name of track",
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
        parser.add_option(
            "--fetch-missing",
            action="store_true",
            help="Fetch missing items from YouTube",
        )

        return parser 

    def _get_track_details(self, artist: str, track: str, url: str | None) -> SingletonDetails | None:
        """Get details for track on YouTube."""
        # If the url has been passed in, use that
        if url:
            return SingletonDetails(title=track, artist=artist, track_url=url)

        # Otherwise perform a search via YTMusic api
        if self.config.get('verbose'):
            print(f'[ytdlp] Searching for {artist} - {track} on YouTube Music')
        ytmusic = YTMusic()
        search_results: list[dict] = ytmusic.search(f'{artist} {track}', filter="songs")
        if not search_results:
            print(f'[ytdlp] No results found for {artist} - {track}')
            print('[ytdlp] Please check the artist and track names and try again.')
            print('[ytdlp] Or consider passing in the url to a track via the --url flag.')
            return None
        song_details: dict = search_results[0]
        return SingletonDetails(
            title=song_details['title'],
            artist=song_details['artists'][0]['name'],
            track_url="https://youtube.com/watch?v=" + song_details['videoId']
        )

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
        available_tracks: bool = all([track['isAvailable'] for track in album_details['tracks']])
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
            'extractaudio': True,
            'outtmpl': self.cache_dir + "/" + ad.artist + "/" + ad.title + "/%(title)s.%(ext)s",
            'postprocessors': [
                {'key': 'FFmpegExtractAudio'},
                {'key': 'FFmpegMetadata'},
            ],
        }

        with YoutubeDL(ydl_opts) as ydl:
            returncode = ydl.download([ad.playlist_url])

        if returncode != 0:
            print(f'[ytdlp] Error downloading {ad}')
            return ""

        return self.cache_dir + "/" + ad.artist + "/" + ad.title

    def _download_singleton(self, sd: SingletonDetails) -> str:
        """Download track from YouTube."""
        if self.config.get('verbose'):
            print(f'[ytdlp] Downloading {sd}')

        ydl_opts = {
            'format': 'bestaudio/best',
            'extractaudio': True,
            'outtmpl': self.cache_dir + "/" + sd.artist + "/%(title)s.%(ext)s",
            'postprocessors': [
                {'key': 'FFmpegExtractAudio'},
                {'key': 'FFmpegMetadata'},
            ],
        }

        with YoutubeDL(ydl_opts) as ydl:
            returncode = ydl.download([sd.track_url])

        if returncode != 0:
            print(f'[ytdlp] Error downloading {sd}')
            return ""

        return self.cache_dir + "/" + sd.artist

    def _import_album(self, lib, album_dir: str) -> str | None:
        """Import album into beets."""
        opts, args = ui.commands.import_cmd.parse_args(["-m", album_dir])
        if os.getenv('BEETS_ENV') == 'develop':
            opts, args = ui.commands.import_cmd.parse_args(
                ["-m", album_dir, "--config", "env.config.yml"],
            )
        if self.config.get('verbose'):
            print("[ytdlp] Running beet import with opts: " + str(opts))

        ui.commands.import_cmd.func(lib, opts, args)

        return album_dir

    def _import_singleton(self, lib, track_dir: str) -> str | None:
        """Import track into beets."""
        opts, args = ui.commands.import_cmd.parse_args(["-m", track_dir])
        if os.getenv('BEETS_ENV') == 'develop':
            opts, args = ui.commands.import_cmd.parse_args(
                ["-m", track_dir, "--config", "env.config.yml"],
            )
        if self.config.get('verbose'):
            print("[ytdlp] Running beet import with opts: " + str(opts))

        ui.commands.import_cmd.func(lib, opts, args)

        return track_dir

    def _list_missing(self, lib) -> list[AlbumDetails]:
        print("Not yet implemented")
        return []

    def _clear_cache(self, d: str) -> None:
        """Clear the cache of downloaded files."""
        os.remove(d)
        

