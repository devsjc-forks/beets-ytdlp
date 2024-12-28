from beets import config
import dataclasses
from beets import ui
from beets.dbcore import types
from beets.library import Item, Library
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

    def __post_init__(self) -> None:
        """Replace any invalid characters."""
        self.name = self.name.replace("/", "-")

@dataclasses.dataclass
class TrackMetadata:
    title: str
    artists: list[ArtistMetadata]
    trackNumber: int
    videoId: str
    album: str
    isAvailable: bool

    def __post_init__(self) -> None:
        """Replace any invalid characters."""
        self.title = self.title.replace("/", "-")
        self.album = self.album.replace("/", "-")

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

    def __post__init__(self) -> None:
        """Replace any invalid characters."""
        self.title = self.title.replace("/", "-")

    def __str__(self) -> str:
        return f'{self.title} by {self.artists[0].name}'

    def url(self) -> str:
        return "https://youtube.com/playlist?list=" + self.audioPlaylistId

    def available_tracks(self) -> list[TrackMetadata]:
        return [track for track in self.tracks if track.isAvailable]

    def track_iterator(self) -> Iterator[TrackMetadata]:
        for track in self.available_tracks():
            yield track

class YTDLPPlugin(BeetsPlugin):
    """A plugin for downloading music from YouTube and importing into beets."""

    config: dict
    cache_dir: pathlib.Path

    item_types: dict[str, types.Type] = {
        'source_url': types.STRING,
        'url': types.STRING,
    }

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
        self.add_media_field(u'source_url', url)

        # Add listener for item moves
        self.register_listener('item_moved', self._on_item_moved)

    def commands(self):
        """Add commands to beets CLI."""

        def ytdlp_func(lib: Library, opts: optparse.Values, args: list[str]) -> None:
            """Download albums from YouTube and import into beets."""
            if self.config.get("verbose"):
                print(f"[ytdlp] Running ytdlp with opts: {opts}")

            # Album download mode
            if opts.artist and opts.album:
                album_details = self._fetch_album_metadata(opts.artist, opts.album, opts.url)
                if not album_details:
                    return

                album_dir: pathlib.Path
                for track in album_details.track_iterator():
                    album_dir = self._download_track_to_cache(track)
                    if not album_dir:
                        return

                self._import_album(lib, album_dir)

                print(f"{Colors.SUCCESS}[ytdlp] Successfully imported {album_details}{Colors.END}")
                return

            # Missing items mode
            if opts.fetch_missing:
                num_albums: int = 0
                for missing_album_tracks in  self._list_missing(lib):
                    print(
                        f"Fetching {missing_album_tracks[0].album} "
                        f"by {missing_album_tracks[0].artists[0].name}",
                    )

                    album_dir: pathlib.Path
                    for track in missing_album_tracks:
                        out = self._download_track_to_cache(track)
                        album_dir = out if out else album_dir

                    if album_dir:
                        self._import_album(lib, album_dir)
                    num_albums += 1
                
                print(f"{Colors.SUCCESS}[ytdlp] Successfully imported {num_albums} albums{Colors.END}")

            else:
                print("\n".join((
                    f"{Colors.WARNING}[ytdlp] Invalid arguments. Please specify either:",
                    "\t--artist and --album",
                    "\t--fetch-missing{Colors.END}",
                )))
                return

        ytdlp_command = ui.Subcommand(
            'ydl',
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
        album_metadata: AlbumMetadata

        if url:
            # If the url has been passed in, use that
            browse_id: str = ytmusic.get_album_browse_id(url)
            playlist_id: str = url.split("list=")[-1]
            playlist_info: dict = ytmusic.get_playlist(playlist_id)

            # Add missing details to playlist info dictionary
            for i, track in enumerate(playlist_info['tracks']):
                playlist_info['tracks'][i]['trackNumber'] = i + 1
                playlist_info["tracks"][i]["album"] = album
            playlist_info['audioPlaylistId'] = playlist_id
            playlist_info['artists'] = [{"name": artist, "id": ""}]
            playlist_info['album'] = album

            album_metadata: AlbumMetadata = dacite.from_dict(AlbumMetadata, playlist_info)

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

        outdir: pathlib.Path = self.cache_dir / track.artists[0].name / track.album

        ydl_opts = {
            'format': 'bestaudio/best',
            'extractaudio': True,
            'verbose': False,
            'quiet': True,
            'outtmpl': f"{outdir.as_posix()}/{track.trackNumber:02d} - {track.title}.%(ext)s",
            'postprocessors': [
                {'key': 'FFmpegExtractAudio'},
                {'key': 'FFmpegMetadata'},
            ],
            'embed-metadata': True,
            "restrictfilenames": True,
            "windowsfilenames": True,
        }

        with YoutubeDL(ydl_opts) as ydl:
            returncode = ydl.download([track.url()])

        if returncode != 0:
            print(f'[ytdlp] Error downloading {track}')
            return None

        # Write the URL to the metadata
        try:
            f: mediafile.MediaFile = mediafile.MediaFile(
                    outdir / f"{track.trackNumber:02d} - {track.title}.opus",
            )
            f.url = track.url()
            f.track = track.trackNumber
            f.save()
        except Exception as e:
            print(f'[ytdlp] Error writing metadata for {track}')
            return None

        return outdir

    def _import_album(self, lib: Library, album_dir: pathlib.Path) -> pathlib.Path | None:
        """Import album into beets."""
        opts, args = ui.commands.import_cmd.parse_args(["-m", album_dir.as_posix()])
        if os.getenv('BEETS_ENV') == 'develop':
            opts, args = ui.commands.import_cmd.parse_args(
                ["-m", album_dir.as_posix(), "--config", "env.config.yml"],
            )
        if self.config.get('verbose'):
            print(f"[ytdlp] Importing {album_dir}")
        ui.commands.import_cmd.func(lib, opts, args)

        return album_dir

    def _on_item_moved(self, item: Item, source: str, destination: str) -> None:
        """Update the source_url field when an item is moved."""
        if self.config.get('verbose'):
            print(f"[ytdlp] Updating source_url for {item}")
        f: mediafile.MediaFile = mediafile.MediaFile(destination)
        item.source_url = f.url
        item.store()

    def _list_missing(self, lib: Library) -> Iterator[list[TrackMetadata]]:
        """List missing items.

        Returns:
            Iterator over list of TrackMetadata objects for each album.
        """
        for album in lib.albums():
            # Download any missing items via their source_url
            item: Item
            album_tracks: list[TrackMetadata] = []
            for item in album.items():
                if not item.filepath.exists() and item.get("source_url"):
                    album_tracks.append(TrackMetadata(
                        title=item.get("title"),
                        artists=[ArtistMetadata(name=item.get("artist"), id="")],
                        trackNumber=item.get("track"),
                        videoId=item.get("source_url").split('v=')[-1],
                        album=item.get("album"),
                        isAvailable=True,
                    ))
            yield album_tracks

    def _clear_cache(self) -> None:
        """Clear the cache of downloaded files."""
        shutil.rmtree(self.cache_dir.as_posix())
        

