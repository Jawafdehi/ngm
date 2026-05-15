"""
Tests for index cleanup — keeps the 7 most recent successful index runs,
deletes everything older. Failed days (missing directories) are handled
organically: they're simply absent from the listing.
"""

import datetime

from unittest.mock import MagicMock

from ngm.index.cleanup import (
    _parse_date_from_dirname,
    _list_date_dirs,
    _delete_old_indices,
)


class TestParseDateFromDirname:
    """Date string parsing from directory names."""

    def test_valid_date(self):
        result = _parse_date_from_dirname("2026-05-15")
        assert result == datetime.datetime(2026, 5, 15)

    def test_invalid_format(self):
        assert _parse_date_from_dirname("not-a-date") is None

    def test_empty_string(self):
        assert _parse_date_from_dirname("") is None

    def test_partial_date(self):
        assert _parse_date_from_dirname("2026-05") is None

    def test_non_iso_format(self):
        assert _parse_date_from_dirname("2026/05/15") is None


class TestListDateDirs:
    """Listing date-indexed directories sorted newest-first."""

    def _make_mock_entry(self, name, is_dir=True):
        entry = MagicMock()
        entry.name = name
        entry.is_dir.return_value = is_dir
        return entry

    def test_sorts_newest_first(self):
        entries = [
            self._make_mock_entry("2026-05-01"),
            self._make_mock_entry("2026-05-15"),
            self._make_mock_entry("2026-05-10"),
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        result = _list_date_dirs(indices_path)

        assert len(result) == 3
        assert result[0][0] == datetime.datetime(2026, 5, 15)
        assert result[1][0] == datetime.datetime(2026, 5, 10)
        assert result[2][0] == datetime.datetime(2026, 5, 1)

    def test_skips_non_directories(self):
        entries = [
            self._make_mock_entry("2026-05-15", is_dir=False),
            self._make_mock_entry("2026-05-10", is_dir=True),
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        result = _list_date_dirs(indices_path)
        assert len(result) == 1
        assert result[0][0] == datetime.datetime(2026, 5, 10)

    def test_skips_unparseable_names(self):
        entries = [
            self._make_mock_entry("not-a-date"),
            self._make_mock_entry("index-v2.json"),
            self._make_mock_entry("2026-05-15"),
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        result = _list_date_dirs(indices_path)
        assert len(result) == 1
        assert result[0][0] == datetime.datetime(2026, 5, 15)

    def test_empty_when_no_indices_dir(self):
        indices_path = MagicMock()
        indices_path.exists.return_value = False

        result = _list_date_dirs(indices_path)
        assert result == []

    def test_empty_when_no_date_dirs(self):
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = []

        result = _list_date_dirs(indices_path)
        assert result == []

    def test_gaps_in_dates(self):
        """Failed days produce gaps — they're simply absent from the listing."""
        entries = [
            self._make_mock_entry("2026-05-15"),
            self._make_mock_entry("2026-05-10"),  # day 11-14 missing (failed)
            self._make_mock_entry("2026-05-05"),
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        result = _list_date_dirs(indices_path)
        assert len(result) == 3
        assert [p[0] for p in result] == [
            datetime.datetime(2026, 5, 15),
            datetime.datetime(2026, 5, 10),
            datetime.datetime(2026, 5, 5),
        ]


class TestDeleteOldIndices:
    """Deletion of indices beyond the MAX_KEEP most recent runs."""

    def _make_mock_entry(self, name, is_dir=True):
        entry = MagicMock()
        entry.name = name
        entry.is_dir.return_value = is_dir
        return entry

    def test_keeps_seven_deletes_rest(self):
        entries = [
            self._make_mock_entry(f"2026-05-{d:02d}")
            for d in range(1, 11)  # 2026-05-01 through 2026-05-10
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)

        # MAX_KEEP = 7, 10 entries total → 3 deleted (05-01, 05-02, 05-03)
        assert deleted == 3
        entries[0].rmtree.assert_called_once()  # 05-01
        entries[1].rmtree.assert_called_once()  # 05-02
        entries[2].rmtree.assert_called_once()  # 05-03
        entries[3].rmtree.assert_not_called()  # 05-04 (kept)

    def test_no_indices_dir_returns_zero(self):
        indices_path = MagicMock()
        indices_path.exists.return_value = False

        deleted = _delete_old_indices(indices_path)
        assert deleted == 0

    def test_fewer_than_max_keep_deletes_nothing(self):
        entries = [
            self._make_mock_entry("2026-05-15"),
            self._make_mock_entry("2026-05-14"),
            self._make_mock_entry("2026-05-10"),
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)
        assert deleted == 0
        for entry in entries:
            entry.rmtree.assert_not_called()

    def test_exactly_max_keep_deletes_nothing(self):
        entries = [self._make_mock_entry(f"2026-05-{d:02d}") for d in range(9, 16)]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)
        assert deleted == 0

    def test_all_expired(self):
        entries = [
            self._make_mock_entry(f"2026-05-{d:02d}") for d in range(1, 9)  # 8 entries
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)

        # MAX_KEEP = 7, 8 entries → 1 deleted (oldest: 05-01)
        assert deleted == 1

    def test_skips_non_directory_entries(self):
        entries = [
            self._make_mock_entry("2026-05-01", is_dir=True),
            self._make_mock_entry("index-v2.json", is_dir=False),
            self._make_mock_entry("2026-05-15", is_dir=True),
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)
        assert deleted == 0  # 2 date dirs, both kept

    def test_skips_unparseable_date_dirs(self):
        entries = [
            self._make_mock_entry("2026-05-01", is_dir=True),
            self._make_mock_entry("not-a-date", is_dir=True),
            self._make_mock_entry("2026-05-15", is_dir=True),
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)
        assert deleted == 0  # 2 date dirs, both kept

    def test_rmtree_failure_recorded(self):
        failing_entry = self._make_mock_entry("2026-05-01")
        failing_entry.rmtree.side_effect = OSError("permission denied")

        entries = [failing_entry] + [
            self._make_mock_entry(f"2026-05-{d:02d}") for d in range(2, 10)
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)
        # 9 entries, MAX_KEEP = 7 → 2 expired (05-01, 05-02)
        # 05-01 rmtree failed → deleted count = 1
        assert deleted == 1

    def test_gaps_from_failed_days(self):
        """3 entries exist over a 10-day span with gaps. Only 3 total, so none deleted."""
        entries = [
            self._make_mock_entry("2026-05-15"),
            self._make_mock_entry("2026-05-13"),
            self._make_mock_entry("2026-05-07"),
        ]
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)
        assert deleted == 0

    def test_gaps_with_excess(self):
        """Gaps from failed days: 10 exist, keep 7, delete 3 oldest."""
        entries = []
        for d in [1, 2, 3, 5, 8, 9, 12, 14, 17, 20]:  # 10 scattered dates in May
            entries.append(self._make_mock_entry(f"2026-05-{d:02d}"))
        indices_path = MagicMock()
        indices_path.exists.return_value = True
        indices_path.iterdir.return_value = entries

        deleted = _delete_old_indices(indices_path)
        # Sorted newest-first: 20, 17, 14, 12, 9, 8, 5 (keep), 3, 2, 1 (delete 3)
        assert deleted == 3
