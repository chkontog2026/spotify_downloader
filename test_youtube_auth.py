import queue
import json
import shlex
import time
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, patch

from yt_dlp import parse_options
import spotdl_gui_v4 as gui


class YouTubeAuthTests(unittest.TestCase):
    def test_packaged_search_uses_console_worker(self):
        executable = str(Path('packaged/SpotDL GUI Pro v4.exe').resolve())
        with patch.object(gui.sys, 'frozen', True, create=True), patch.object(gui.sys, 'executable', executable):
            command = gui.yt_dlp_command()
        self.assertEqual(command, [str(Path(executable).parent / 'spotdl-cli.exe'), '--yt-dlp'])

    def test_search_reads_metadata_without_extracting_every_video(self):
        result = SimpleNamespace(stdout=json.dumps({
            'id': 'example', 'title': 'Artist - Song', 'duration': 213,
            'channel': 'Artist - Topic',
        }))
        with patch.object(gui.subprocess, 'run', return_value=result) as run:
            candidates = gui.SpotDLApp._youtube_candidates(SimpleNamespace(), 'Artist Song')
        self.assertIn('--flat-playlist', run.call_args.args[0])
        self.assertEqual(candidates[0]['title'], 'Artist - Song')
        self.assertEqual(candidates[0]['duration'], '213')
        self.assertEqual(candidates[0]['url'], 'https://www.youtube.com/watch?v=example')

    def test_browser_uses_authenticated_default_clients(self):
        for browser in ('firefox', 'chrome', 'edge'):
            opts = parse_options(shlex.split(gui.repair_ytdlp_args(browser))).ydl_opts
            self.assertEqual(opts['cookiesfrombrowser'][0], browser)
            self.assertFalse(opts.get('extractor_args', {}).get('youtube', {}).get('player_client'))
        self.assertEqual(gui.repair_ytdlp_args(), gui.YT_DLP_COMPAT_ARGS)

    def test_failed_download_with_zero_exit_is_not_success(self):
        app = SimpleNamespace(
            events=queue.Queue(), root_var=Mock(get=lambda: '.'),
            url_var=Mock(get=lambda: 'https://open.spotify.com/track/test'),
            format_var=Mock(get=lambda: 'mp3'), last_album_dir=None,
            _update_tracks_from_raw_line=Mock(),
        )
        process = Mock(stdout=iter(['ERROR: Sign in to confirm your age\n']))
        process.wait.return_value = 0
        with patch.object(gui.subprocess, 'Popen', return_value=process) as popen:
            gui.SpotDLApp._run_repair_selected(
                app, 'track', SimpleNamespace(url='https://open.spotify.com/track/test'),
                time.monotonic(), 'https://www.youtube.com/watch?v=B4l53EBAKKo',
                youtube_browser='firefox', bitrate='256k',
            )
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index('--bitrate') + 1], '256k')
        self.assertIn('--cookies-from-browser firefox', command[command.index('--yt-dlp-args') + 1])
        events = list(app.events.queue)
        self.assertEqual(events[-1][0], 'repair_done')
        self.assertEqual(events[-1][1][1], 1)
        self.assertTrue(any('επιβεβαίωση ηλικίας' in str(event) for event in events))


if __name__ == '__main__':
    unittest.main()
