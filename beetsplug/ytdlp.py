from beets import config
import dataclasses
from beets import ui
from beets.plugins import BeetsPlugin
import optparse
import mediafile
from ytmusicapi import YTMusic
from yt_dlp import YoutubeDL
import shutil
import os
import pathlib
import dacite
from collections.abc import Iterator

class Colors():
    INFO = '\033[94m'
    SUCCESS = '\033[92m'
    WARNING = '\033[93m'
    BOLD = '\033[1m'
    END = '\033[0m'

@dataclasses.dataclass
class ArtistMetadata:
    name: str
    id: str

@dataclasses.dataclass
class TrackMetadata:
    title: str
    artists: list[ArtistMetadata]
    trackNumber: int
    videoId: str
    album: str
    isAvailable: bool

    def url(self) -> str:
        return "https://youtube.com/watch?v=" + self.videoId

    def __str__(self) -> str:
        return f'{self.title} by {self.artists[0].name}'

@dataclasses.dataclass
class AlbumMetadata:
    title: str
    artists: list[ArtistMetadata]
    trackCount: int
    audioPlaylistId: str
    tracks: list[TrackMetadata]

    def url(self) -> str:
        return "https://youtube.com/playlist?list=" + self.audioPlaylistId

    def __str__(self) -> str:
        return f'{self.title} by {self.artists[0].name}'

    def available_tracks(self) -> list[TrackMetadata]:
        return [track for track in self.tracks if track.isAvailable]

    def track_iterator(self) -> Iterator[TrackMetadata]:
        for track in self.available_tracks():
            yield track

class YTDLPPlugin(BeetsPlugin):
    """A plugin for downloading music from YouTube and importing into beets."""

    config: dict
    cache_dir: pathlib.Path

    def __init__(self, *args, **kwargs) -> None:
        """Set default values."""

        super(YTDLPPlugin, self).__init__()

        self.cache_dir: pathlib.Path = pathlib.Path(config["directory"].get() + "/.import")

        # Default options
        self._config = {'verbose': False}
        self._config.update(self.config)
        self.config = self._config

        # Be verbose if beets is verbose
        if not self.config.get('verbose'):
            self.config['verbose'] = True

        # Add URK tag as custom metadata field
        # See
        # - https://discourse.beets.io/t/how-to-use-custom-fields/202
        # - https://beets.readthedocs.io/en/stable/dev/plugins.html#extend-mediafile
        # - https://github.com/beetbox/mediafile/blob/e1de3640e253ff88f00e8495d3b7626ff6b3e2b8/mediafile.py#L1845C5-L1850C6
        url: mediafile.MediaField = mediafile.MediaField(
            mediafile.MP3DescStorageStyle(key='WXXX', attr='url', multispec=False),
            mediafile.MP4StorageStyle('\xa9url'),
            mediafile.StorageStyle('URL'),
            mediafile.ASFStorageStyle('WM/URL'),
        )
        self.add_media_field(u'url', url)

    def commands(self):
        """Add commands to beets CLI."""

        def ytdlp_func(lib, opts: optparse.Values, args: list[str]) -> None:
            """Download albums from YouTube and import into beets."""
            if self.config.get("verbose"):
                print(f"[ytdlp] Running ytdlp with opts: {opts}")

            # Album download mode
            if opts.artist and opts.album:
                album_details = self._fetch_album_metadata(opts.artist, opts.album, opts.url)
                if not album_details:
                    return

                for track in album_details.track_iterator():
                    album_dir = self._download_track_to_cache(track)
                    if not album_dir:
                        return

                self._import_album(lib, album_dir)

                print(f"{Colors.SUCCESS}[ytdlp] Successfully imported {album_details}{Colors.END}")
                return

            # Missing items mode
            if opts.fetch_missing:
                print("Not yet implemented")

            else:
                print("\n".join((
                    f"{Colors.WARNING}[ytdlp] Invalid arguments. Please specify either:",
                    "\t--artist and --album",
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

    def _fetch_album_metadata(self, artist: str, album: str, url: str | None) -> AlbumMetadata | None:
        """Get details for album playlist on YouTube."""
        ytmusic = YTMusic()

        if url:
            # If the url has been passed in, use that
            browse_id: str = ytmusic.get_album_browse_id(url)
        else:
            # Otherwise perform a search via YTMusic api
            if self.config.get('verbose'):
                print(f'[ytdlp] Searching for {artist} - {album} on YouTube Music')
            search_results: list[dict] = ytmusic.search(f'{artist} {album}', filter="albums")
            if not search_results:
                print(f'[ytdlp] No results found for {artist} - {album}')
                print('[ytdlp] Please check the artist and album names and try again.')
                print('[ytdlp] Or consider passing in the url to a playlist via the --url flag.')
                return None
            # Take first result
            browse_id = search_results[0]['browseId']

        album_metadata: AlbumMetadata = dacite.from_dict(
            AlbumMetadata,
            ytmusic.get_album(browse_id),
        )
        
        if len(album_metadata.available_tracks()) < album_metadata.trackCount:
            print(f'[ytdlp] Not all tracks are available for {album} by {artist}')
            print("[ytdlp] Consider passing in the url to a playlist via the --url flag.")
            return None

        return album_metadata

    def _download_track_to_cache(self, track: TrackMetadata) -> pathlib.Path | None:
        """Download track to cache."""

        if self.config.get('verbose'):
            print(f'[ytdlp] Downloading {track}')

        outdir = self.cache_dir / track.artists[0].name / track.album

        ydl_opts = {
            'format': 'bestaudio/best',
            'extractaudio': True,
            'outtmpl': f"{outdir.as_posix()}/{track.title}.%(ext)s",
            'postprocessors': [
                {'key': 'FFmpegExtractAudio'},
                {'key': 'FFmpegMetadata'},
            ],
        }

        with YoutubeDL(ydl_opts) as ydl:
            returncode = ydl.download([track.url()])

        if returncode != 0:
            print(f'[ytdlp] Error downloading {track}')
            return None

        # Write the URL to the metadata
        f: mediafile.MediaFile = mediafile.MediaFile(outdir / f"{track.title}.opus")
        f.url = track.url()
        f.save()

        return outdir

    def _import_album(self, lib, album_dir: pathlib.Path) -> pathlib.Path | None:
        """Import album into beets."""
        opts, args = ui.commands.import_cmd.parse_args(["-m", album_dir.as_posix()])
        if os.getenv('BEETS_ENV') == 'develop':
            opts, args = ui.commands.import_cmd.parse_args(
                ["-m", album_dir.as_posix(), "--config", "env.config.yml"],
            )
        if self.config.get('verbose'):
            print("[ytdlp] Running beet import with opts: " + str(opts))

        ui.commands.import_cmd.func(lib, opts, args)

        return album_dir

    def _list_missing(self, lib) -> list[AlbumMetadata]:
        print("Not yet implemented")
        return []

    def _clear_cache(self, d: pathlib.Path) -> None:
        """Clear the cache of downloaded files."""
        shutil.rmtree(d.as_posix())
        

