"""Characterization tests for doctor.checks.repair.dead_symlinks.

Lock in the current behavior of:
  - _radarr_dead_files(): filtering / yielding dead movie files
  - _sonarr_dead_files(): filtering / yielding dead episode files per season
  - _repair_radarr_movie(): delete + toggle + search orchestration
  - _repair_sonarr_season(): delete + toggle + SeasonSearch orchestration

All filesystem access (_dead_symlink) is patched so tests run without real
symlinks.  Arr instances are MagicMock.  Config globals are patched on the
dead_symlinks module (they arrive via star-import).
"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from doctor.checks.repair.dead_symlinks import (
    _radarr_dead_files,
    _sonarr_dead_files,
    _repair_radarr_movie,
    _repair_sonarr_season,
)

# Module path prefix for patching star-imported names
_MOD = "doctor.checks.repair.dead_symlinks"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _movie(mid, title, path, monitored=True, mfid=None):
    """Minimal Radarr movie dict."""
    mf = {"path": path}
    if mfid is not None:
        mf["id"] = mfid
    return {"id": mid, "title": title, "monitored": monitored, "movieFile": mf}


def _series(sid, title, monitored=True):
    return {"id": sid, "title": title, "monitored": monitored}


def _efile(efid, path, season_number=None):
    """Minimal Sonarr episode file dict."""
    ef = {"id": efid, "path": path}
    if season_number is not None:
        ef["seasonNumber"] = season_number
    return ef


def _episode(epid, efid, season_number, episode_number=None):
    """Minimal Sonarr episode dict."""
    return {"id": epid, "episodeFileId": efid, "seasonNumber": season_number, "episodeNumber": episode_number}


def _make_arr(name="sonarr-1", kind="sonarr"):
    arr = MagicMock()
    arr.name = name
    arr.kind = kind
    return arr


# ---------------------------------------------------------------------------
# _radarr_dead_files
# ---------------------------------------------------------------------------

class RadarrDeadFilesTest(unittest.TestCase):

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_yields_dead_movie(self, _ds):
        movies = [_movie(1, "Dead Movie", "/lib/dead.mkv", mfid=10)]
        result = list(_radarr_dead_files(movies))
        self.assertEqual(result, [(1, "Dead Movie", 10)])

    @patch(_MOD + "._dead_symlink", return_value=False)
    def test_skips_live_symlink(self, _ds):
        movies = [_movie(1, "Live Movie", "/lib/live.mkv", mfid=10)]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_UNMONITORED", False)
    def test_skips_unmonitored_when_flag_off(self, _ds):
        movies = [_movie(1, "Unmon Movie", "/lib/x.mkv", monitored=False, mfid=10)]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_UNMONITORED", True)
    def test_includes_unmonitored_when_flag_on(self, _ds):
        movies = [_movie(1, "Unmon Movie", "/lib/x.mkv", monitored=False, mfid=10)]
        result = list(_radarr_dead_files(movies))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 1)

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_LIBS", ["/allowed/"])
    def test_repair_libs_filters_path(self, _ds):
        movies = [
            _movie(1, "Allowed", "/allowed/a.mkv", mfid=10),
            _movie(2, "Blocked", "/other/b.mkv", mfid=20),
        ]
        result = list(_radarr_dead_files(movies))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], 1)

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_LIBS", [])
    def test_empty_repair_libs_allows_all(self, _ds):
        """Empty REPAIR_LIBS means no path filter -> all paths pass."""
        movies = [_movie(1, "Any", "/any/path.mkv", mfid=10)]
        self.assertEqual(len(list(_radarr_dead_files(movies))), 1)

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_movie_without_id(self, _ds):
        movies = [{"title": "No ID", "movieFile": {"path": "/x.mkv"}}]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_movie_without_file_path(self, _ds):
        movies = [{"id": 1, "title": "No Path", "movieFile": {}}]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_movie_with_no_movie_file(self, _ds):
        movies = [{"id": 1, "title": "No File"}]
        self.assertEqual(list(_radarr_dead_files(movies)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_title_truncated_to_70_chars(self, _ds):
        long_title = "A" * 100
        movies = [_movie(1, long_title, "/lib/x.mkv", mfid=10)]
        result = list(_radarr_dead_files(movies))
        self.assertEqual(len(result[0][1]), 70)


# ---------------------------------------------------------------------------
# _sonarr_dead_files
# ---------------------------------------------------------------------------

class SonarrDeadFilesTest(unittest.TestCase):

    def _setup_arr(self, efiles, eps):
        arr = _make_arr()
        arr.episode_files.return_value = efiles
        arr.episodes.return_value = eps
        return arr

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_yields_dead_episode_files_grouped_by_season(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1),
                  _efile(101, "/lib/S01E02.mkv", season_number=1)]
        eps = [_episode(10, 100, 1), _episode(11, 101, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]

        result = list(_sonarr_dead_files(arr, series))
        self.assertEqual(len(result), 1)
        sid, title, sn, efids, _series_dict, _epids = result[0]
        self.assertEqual(sid, 5)
        self.assertEqual(sn, 1)
        self.assertCountEqual(efids, [100, 101])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_multiple_seasons_yield_separately(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1),
                  _efile(200, "/lib/S02E01.mkv", season_number=2)]
        eps = [_episode(10, 100, 1), _episode(20, 200, 2)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]

        result = list(_sonarr_dead_files(arr, series))
        self.assertEqual(len(result), 2)
        seasons = {r[2] for r in result}
        self.assertEqual(seasons, {1, 2})

    @patch(_MOD + "._dead_symlink", return_value=False)
    def test_skips_live_files(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1)]
        eps = [_episode(10, 100, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_UNMONITORED", False)
    def test_skips_unmonitored_series(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1)]
        eps = [_episode(10, 100, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "UnmonShow", monitored=False)]
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_UNMONITORED", True)
    def test_includes_unmonitored_series_when_flag_on(self, _ds):
        efiles = [_efile(100, "/lib/S01E01.mkv", season_number=1)]
        eps = [_episode(10, 100, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "UnmonShow", monitored=False)]
        self.assertEqual(len(list(_sonarr_dead_files(arr, series))), 1)

    @patch(_MOD + "._dead_symlink", return_value=True)
    @patch(_MOD + ".REPAIR_LIBS", ["/allowed/"])
    def test_repair_libs_filters_episode_paths(self, _ds):
        efiles = [_efile(100, "/allowed/S01E01.mkv", season_number=1),
                  _efile(101, "/other/S01E02.mkv", season_number=1)]
        eps = [_episode(10, 100, 1), _episode(11, 101, 1)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]

        result = list(_sonarr_dead_files(arr, series))
        # Only the /allowed/ file should be included
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][3], [100])
        self.assertEqual(result[0][5], [10])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_season_from_episode_cross_reference(self, _ds):
        """Episode file without seasonNumber falls back to episode cross-reference."""
        efiles = [_efile(100, "/lib/S01E01.mkv")]  # no seasonNumber on the file
        eps = [_episode(10, 100, 1)]  # episode knows season 1
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]

        result = list(_sonarr_dead_files(arr, series))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][2], 1)  # season number from cross-reference
        self.assertEqual(result[0][5], [10])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_file_without_id(self, _ds):
        efiles = [{"path": "/lib/S01E01.mkv", "seasonNumber": 1}]  # no "id"
        eps = []
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_file_without_season(self, _ds):
        """File with no seasonNumber and no cross-reference is skipped."""
        efiles = [_efile(100, "/lib/orphan.mkv")]  # no seasonNumber
        eps = []  # no episode cross-reference either
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_continues_on_episode_files_exception(self, _ds):
        """If arr.episode_files() raises, the series is skipped silently."""
        arr = _make_arr()
        arr.episode_files.side_effect = Exception("API error")
        arr.episodes.return_value = []
        series = [_series(5, "Show"), _series(6, "Show2")]

        # The generator should not raise; it just skips the broken series
        result = list(_sonarr_dead_files(arr, series))
        self.assertEqual(result, [])

    @patch(_MOD + "._dead_symlink", return_value=True)
    def test_skips_series_without_id(self, _ds):
        series = [{"title": "No ID", "monitored": True}]
        arr = _make_arr()
        self.assertEqual(list(_sonarr_dead_files(arr, series)), [])


# ---------------------------------------------------------------------------
# _repair_radarr_movie
# ---------------------------------------------------------------------------

class RepairRadarrMovieTest(unittest.TestCase):

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_deletes_file_toggles_monitor_and_searches(self):
        arr = _make_arr("radarr-1", "radarr")
        arr.command.return_value = 42

        result = _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10)
        self.assertTrue(result)
        arr.delete_file.assert_called_once_with(10)
        arr.set_monitored.assert_any_call([1], False)
        arr.set_monitored.assert_any_call([1], True)
        arr.command.assert_called_once_with("MoviesSearch", movieIds=[1])

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_skips_delete_when_no_mfid(self):
        arr = _make_arr("radarr-1", "radarr")
        arr.command.return_value = 42

        _repair_radarr_movie(arr, mid=1, title="Movie", mfid=None)
        arr.delete_file.assert_not_called()
        # toggle and search still happen
        arr.command.assert_called_once()

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", True)
    def test_dry_run_does_not_call_arr(self):
        arr = _make_arr("radarr-1", "radarr")

        result = _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10)
        self.assertTrue(result)
        arr.delete_file.assert_not_called()
        arr.set_monitored.assert_not_called()
        arr.command.assert_not_called()

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_monitor_toggle_failure_does_not_abort(self):
        """If set_monitored raises, repair still issues the search command."""
        arr = _make_arr("radarr-1", "radarr")
        arr.set_monitored.side_effect = Exception("API down")
        arr.command.return_value = 42

        result = _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10)
        self.assertTrue(result)
        arr.command.assert_called_once_with("MoviesSearch", movieIds=[1])

    @patch(_MOD + "._repair_record_verify")
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_verify_records_when_enabled(self, mock_verify):
        arr = _make_arr("radarr-1", "radarr")
        arr.command.return_value = 42
        state = {}

        _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10, state=state)
        mock_verify.assert_called_once_with(state, arr, "Movie", 42, 1, [1], hierarchical=False)

    @patch(_MOD + "._repair_record_verify")
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_verify_skipped_when_state_is_none(self, mock_verify):
        arr = _make_arr("radarr-1", "radarr")
        arr.command.return_value = 42

        _repair_radarr_movie(arr, mid=1, title="Movie", mfid=10, state=None)
        mock_verify.assert_not_called()


# ---------------------------------------------------------------------------
# _repair_sonarr_season
# ---------------------------------------------------------------------------

class RepairSonarrSeasonTest(unittest.TestCase):

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_deletes_all_efids_and_searches_season(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [
            {"id": 10, "seasonNumber": 1},
            {"id": 11, "seasonNumber": 1},
            {"id": 20, "seasonNumber": 2},
        ]
        arr.command.return_value = 99

        result = _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                                       efids=[100, 101])
        self.assertTrue(result)
        # Both episode files deleted
        self.assertEqual(arr.delete_file.call_count, 2)
        arr.delete_file.assert_any_call(100)
        arr.delete_file.assert_any_call(101)
        # Monitor toggle on episodes for season 1 only
        arr.set_monitored.assert_any_call([10, 11], False)
        arr.set_monitored.assert_any_call([10, 11], True)
        # Season search
        arr.command.assert_called_once_with("SeasonSearch", seriesId=5, seasonNumber=1)

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", True)
    def test_dry_run_does_not_call_arr(self):
        arr = _make_arr("sonarr-1", "sonarr")

        result = _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                                       efids=[100, 101])
        self.assertTrue(result)
        arr.delete_file.assert_not_called()
        arr.set_monitored.assert_not_called()
        arr.command.assert_not_called()

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_monitor_toggle_failure_does_not_abort(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}]
        arr.set_monitored.side_effect = Exception("API down")
        arr.command.return_value = 99

        result = _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                                       efids=[100])
        self.assertTrue(result)
        arr.command.assert_called_once_with("SeasonSearch", seriesId=5, seasonNumber=1)

    @patch(_MOD + "._repair_record_verify")
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_verify_records_when_enabled(self, mock_verify):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}]
        arr.command.return_value = 99
        state = {}

        _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                              efids=[100], state=state)
        mock_verify.assert_called_once_with(state, arr, "Show", 99, 5, [10],
                                                          strategy='season', season_number=1, series_id=5,
                                                          hierarchical=False)

    @patch(_MOD + "._repair_record_verify")
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_verify_skipped_when_state_is_none(self, mock_verify):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = []
        arr.command.return_value = 99

        _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                              efids=[100], state=None)
        mock_verify.assert_not_called()

    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_empty_epids_still_searches(self):
        """If no episodes match the season (edge case), toggle is skipped but search runs."""
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = []  # no episodes for this season
        arr.command.return_value = 99

        result = _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                                       efids=[100])
        self.assertTrue(result)
        arr.set_monitored.assert_not_called()
        arr.command.assert_called_once()


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Hierarchical search strategy
# ---------------------------------------------------------------------------

class HierarchicalSearchStrategyTest(unittest.TestCase):
    """Test the smart command selection for dead seasons based on airing status."""

    @patch(_MOD + ".REPAIR_HIERARCHICAL_SEARCH", True)
    def test_ended_show_uses_series_search(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}]
        arr.command.return_value = 99
        series = {"id": 5, "title": "Ended Show", "ended": True}

        _repair_sonarr_season(arr, sid=5, title="Ended Show", season_number=1,
                              efids=[100], state=None, series=series)
        arr.command.assert_called_once_with("SeriesSearch", seriesId=5)

    @patch(_MOD + ".REPAIR_HIERARCHICAL_SEARCH", True)
    def test_continuing_show_with_ended_season_uses_season_search(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}]
        arr.command.return_value = 99
        # previousAiring 30 days ago -> season ended
        prev = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        series = {
            "id": 5, "title": "Continuing Show", "ended": False,
            "seasons": [{"seasonNumber": 1, "statistics": {"previousAiring": prev}}]
        }

        _repair_sonarr_season(arr, sid=5, title="Continuing Show", season_number=1,
                              efids=[100], state=None, series=series)
        arr.command.assert_called_once_with("SeasonSearch", seriesId=5, seasonNumber=1)

    @patch(_MOD + ".REPAIR_HIERARCHICAL_SEARCH", True)
    def test_continuing_show_with_ongoing_season_uses_episode_search(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}, {"id": 11, "seasonNumber": 1}]
        arr.command.return_value = 99
        # previousAiring 1 day ago -> ongoing season
        prev = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        series = {
            "id": 5, "title": "Continuing Show", "ended": False,
            "seasons": [{"seasonNumber": 1, "statistics": {"previousAiring": prev}}]
        }

        _repair_sonarr_season(arr, sid=5, title="Continuing Show", season_number=1,
                              efids=[100], state=None, series=series)
        arr.command.assert_called_once_with("EpisodeSearch", episodeIds=[10, 11])

    @patch(_MOD + ".REPAIR_HIERARCHICAL_SEARCH", False)
    def test_disabled_hierarchical_defaults_to_season_search(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}]
        arr.command.return_value = 99
        series = {"id": 5, "title": "Show", "ended": True}

        _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                              efids=[100], state=None, series=series)
        arr.command.assert_called_once_with("SeasonSearch", seriesId=5, seasonNumber=1)

    @patch(_MOD + ".REPAIR_HIERARCHICAL_SEARCH", True)
    @patch(_MOD + ".REPAIR_VERIFY", True)
    @patch(_MOD + ".DRY_RUN", False)
    def test_records_strategy_and_season_in_verify(self, *_):
        from doctor.checks.repair.verify import _repair_record_verify
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [{"id": 10, "seasonNumber": 1}]
        arr.command.return_value = 99
        state = {}
        series = {"id": 5, "title": "Ended Show", "ended": True}

        _repair_sonarr_season(arr, sid=5, title="Ended Show", season_number=1,
                              efids=[100], state=state, series=series)
        arr.command.assert_called_once_with("SeriesSearch", seriesId=5)
        # verify state should record strategy and season
        pv = state.get("__repair_verify__", {})
        key = "sonarr-1:ended_show:s01"
        self.assertIn(key, pv)
        self.assertEqual(pv[key]["strategy"], "series")
        self.assertEqual(pv[key]["season_number"], 1)
        self.assertEqual(pv[key]["series_id"], 5)
        self.assertTrue(pv[key].get("hierarchical"))


# ---------------------------------------------------------------------------
# Janitor-reported dead files
# ---------------------------------------------------------------------------

class JanitorDeadFilesTest(unittest.TestCase):

    def _setup_arr(self, efiles, eps):
        arr = _make_arr()
        arr.episode_files.return_value = efiles
        arr.episodes.return_value = eps
        return arr

    @patch(_MOD + "._dead_symlink", return_value=False)
    def test_janitor_dead_file_without_episode_file_record(self, _ds):
        """When the janitor has already removed the file, _sonarr_dead_files should still
        detect the missing episode from the quarantined orig path in the state."""
        efiles = []  # episode file already deleted
        eps = [_episode(10, None, 1, episode_number=2)]  # no episodeFileId
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show", monitored=True)]
        series[0]["path"] = "/lib/shows/Show"
        state = {
            "__janitor_dead_files__": {
                "RELEASE/Show.S01E02.1080p.mkv": {
                    "ts": 0,
                    "orig": "/lib/shows/Show/Season 01/Show - S01E02.mkv",
                    "target": "/mnt/zurg/__all__/RELEASE/Show.S01E02.1080p.mkv",
                }
            }
        }

        result = list(_sonarr_dead_files(arr, series, state=state))
        self.assertEqual(len(result), 1)
        sid, title, sn, efids, _series_dict, epids = result[0]
        self.assertEqual(sid, 5)
        self.assertEqual(sn, 1)
        self.assertEqual(efids, [])
        self.assertEqual(epids, [10])

    @patch(_MOD + "._dead_symlink", return_value=False)
    def test_janitor_dead_file_fallback_by_release_name(self, _ds):
        """When the janitor entry has no orig path, the repair check can still find the
        episode by guessing the series from the release name and parsing the filename."""
        efiles = []
        eps = [_episode(10, None, 1, episode_number=2)]
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Mr. Robot", monitored=True)]
        series[0]["sortTitle"] = "mrrobot"
        state = {
            "__janitor_dead_files__": {
                "Mr.Robot.S01-S04.1080p.BluRay.DD5.1.x264-MIXED/Mr.Robot.S01E02.1080p.BluRay.DTS.x264-SbR.mkv": {
                    "ts": 0,
                    "orig": None,
                    "target": None,
                }
            }
        }
        result = list(_sonarr_dead_files(arr, series, state=state))
        self.assertEqual(len(result), 1)
        sid, title, sn, efids, _series_dict, epids = result[0]
        self.assertEqual(sid, 5)
        self.assertEqual(sn, 1)
        self.assertEqual(efids, [])
        self.assertEqual(epids, [10])

    @patch(_MOD + "._dead_symlink", return_value=False)
    def test_janitor_dead_file_ignored_without_orig_path(self, _ds):
        efiles = []
        eps = []
        arr = self._setup_arr(efiles, eps)
        series = [_series(5, "Show")]
        state = {
            "__janitor_dead_files__": {
                "RELEASE/Show.S01E02.1080p.mkv": {
                    "ts": 0,
                    "orig": None,
                    "target": None,
                }
            }
        }
        result = list(_sonarr_dead_files(arr, series, state=state))
        self.assertEqual(result, [])

    @patch(_MOD + "._dead_symlink", return_value=False)
    def test_janitor_dead_file_matches_symlink_target(self, _ds):
        """A live-looking symlink whose target is recorded as dead by the janitor is treated as dead."""
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as libdir:
            fp = os.path.join(libdir, "Show - S01E02.mkv")
            os.symlink("/mnt/zurg/__all__/RELEASE/Show.S01E02.1080p.mkv", fp)
            efiles = [_efile(100, fp, season_number=1)]
            eps = [_episode(10, 100, 1, episode_number=2)]
            arr = self._setup_arr(efiles, eps)
            series = [_series(5, "Show")]
            series[0]["path"] = libdir
            state = {
                "__janitor_dead_files__": {
                    "RELEASE/Show.S01E02.1080p.mkv": {
                        "ts": 0,
                        "orig": fp,
                        "target": "/mnt/zurg/__all__/RELEASE/Show.S01E02.1080p.mkv",
                    }
                }
            }
            result = list(_sonarr_dead_files(arr, series, state=state))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0][3], [100])
            self.assertEqual(result[0][5], [10])


class ParseJanitorDeadPathTest(unittest.TestCase):

    def test_parses_standard_path(self):
        from doctor.checks.repair.dead_symlinks import _parse_janitor_dead_path
        series = [{"id": 5, "title": "Show", "path": "/lib/shows/Show (2020) {imdb-tt123}"}]
        orig = "/lib/shows/Show (2020) {imdb-tt123}/Season 02/Show (2020) - S02E05.mkv"
        ser, sn, eps = _parse_janitor_dead_path(orig, series)
        self.assertEqual(ser["id"], 5)
        self.assertEqual(sn, 2)
        self.assertEqual(eps, [5])

    def test_multi_episode_filename(self):
        from doctor.checks.repair.dead_symlinks import _parse_janitor_dead_path
        series = [{"id": 5, "title": "Show", "path": "/lib/shows/Show"}]
        orig = "/lib/shows/Show/Season 01/Show - S01E01-E02.mkv"
        ser, sn, eps = _parse_janitor_dead_path(orig, series)
        self.assertEqual(sn, 1)
        self.assertEqual(eps, [1, 2])

    def test_no_match(self):
        from doctor.checks.repair.dead_symlinks import _parse_janitor_dead_path
        series = [{"id": 5, "title": "Show", "path": "/lib/shows/Show"}]
        self.assertIsNone(_parse_janitor_dead_path("/other/path/Show/Season 01/Show - S01E01.mkv", series))


class RepairSonarrSeasonEpidsTest(unittest.TestCase):

    @patch(_MOD + ".REPAIR_HIERARCHICAL_SEARCH", True)
    @patch(_MOD + ".REPAIR_VERIFY", False)
    @patch(_MOD + ".DRY_RUN", False)
    def test_passed_epids_used_for_episode_search(self):
        arr = _make_arr("sonarr-1", "sonarr")
        arr.episodes.return_value = [
            {"id": 10, "seasonNumber": 1},
            {"id": 11, "seasonNumber": 1},
        ]
        arr.command.return_value = 99
        series = {"id": 5, "title": "Show", "ended": False, "seasons": []}

        _repair_sonarr_season(arr, sid=5, title="Show", season_number=1,
                              efids=[], state=None, series=series, epids=[10])
        arr.command.assert_called_once_with("EpisodeSearch", episodeIds=[10])



class GuessSeriesFromReleaseTest(unittest.TestCase):

    def test_exact_match(self):
        from doctor.checks.repair.dead_symlinks import _guess_series_from_release
        series = [{"id": 5, "title": "Mr. Robot", "sortTitle": "mrrobot"}]
        ser = _guess_series_from_release("Mr.Robot.S01-S04.1080p.BluRay.DD5.1.x264-MIXED", series)
        self.assertEqual(ser["id"], 5)

    def test_dotted_title_normalized(self):
        from doctor.checks.repair.dead_symlinks import _guess_series_from_release
        series = [{"id": 5, "title": "My Dress-Up Darling", "sortTitle": "my dress up darling"}]
        ser = _guess_series_from_release("My.Dress-Up.Darling.S02.1080p.BluRay.Remux.DUAL.FLAC.2.0.AVC-DemiHuman", series)
        self.assertEqual(ser["id"], 5)

    def test_no_match(self):
        from doctor.checks.repair.dead_symlinks import _guess_series_from_release
        series = [{"id": 5, "title": "Other Show", "sortTitle": "other show"}]
        ser = _guess_series_from_release("Mr.Robot.S01.1080p.mkv", series)
        self.assertIsNone(ser)


class ParseEpisodesFromFilenameTest(unittest.TestCase):

    def test_single_episode(self):
        from doctor.checks.repair.dead_symlinks import _parse_episodes_from_filename
        self.assertEqual(_parse_episodes_from_filename("Show.S01E05.1080p.mkv", 1), [5])

    def test_episode_range(self):
        from doctor.checks.repair.dead_symlinks import _parse_episodes_from_filename
        self.assertEqual(_parse_episodes_from_filename("Show.S01E01-E02.1080p.mkv", 1), [1, 2])
        self.assertEqual(_parse_episodes_from_filename("Show.S01E01E02.1080p.mkv", 1), [1, 2])

    def test_different_season_ignored(self):
        from doctor.checks.repair.dead_symlinks import _parse_episodes_from_filename
        self.assertEqual(_parse_episodes_from_filename("Show.S02E05.1080p.mkv", 1), [])
