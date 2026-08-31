from __future__ import annotations

import sys
from pathlib import Path, PureWindowsPath

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging/tools"))

from windows_publication import (  # noqa: E402
    FileIdentity,
    WindowsPublicationError,
    flush_directory,
    rename_exclusive,
)


class FakePublicationApi:
    def __init__(self) -> None:
        self.names: dict[str, FileIdentity] = {
            r"C:\LVT\app\candidate": FileIdentity(7, 101),
        }
        self.calls: list[tuple[object, ...]] = []

    def open_parent_chain(self, path: PureWindowsPath) -> list[object]:
        handles = [("directory", index, str(path)) for index in range(len(path.parts))]
        self.calls.append(("open_parent_chain", str(path)))
        return handles

    def open_source(self, parent: object, name: str) -> object:
        path = str(PureWindowsPath(parent[2]) / name)  # type: ignore[index]
        self.calls.append(("open_source", path))
        if path not in self.names:
            raise FileNotFoundError(path)
        return ("source", path, self.names[path])

    def handle_identity(self, handle: object) -> FileIdentity:
        return handle[2]  # type: ignore[index]

    def named_identity(self, parent: object, name: str) -> FileIdentity | None:
        path = str(PureWindowsPath(parent[2]) / name)  # type: ignore[index]
        self.calls.append(("named_identity", path))
        return self.names.get(path)

    def rename_handle_exclusive(
        self,
        source: object,
        destination_parent: object,
        destination_name: str,
    ) -> None:
        source_path = source[1]  # type: ignore[index]
        destination = str(
            PureWindowsPath(destination_parent[2]) / destination_name  # type: ignore[index]
        )
        self.calls.append(("rename", source_path, destination))
        if destination in self.names:
            raise FileExistsError(destination)
        identity = source[2]  # type: ignore[index]
        if self.names.get(source_path) == identity:
            del self.names[source_path]
        self.names[destination] = identity

    def flush_directory(self, handle: object) -> None:
        self.calls.append(("flush", handle))

    def close_handle(self, handle: object) -> None:
        self.calls.append(("close", handle))


SOURCE = PureWindowsPath(r"C:\LVT\app\candidate")
DESTINATION = PureWindowsPath(r"C:\LVT\app\current")


def test_directory_flush_uses_bound_handle_and_closes_chain() -> None:
    api = FakePublicationApi()

    flush_directory(SOURCE.parent, api)

    flush_index = next(index for index, call in enumerate(api.calls) if call[0] == "flush")
    close_indices = [index for index, call in enumerate(api.calls) if call[0] == "close"]
    assert close_indices
    assert flush_index < min(close_indices)


def test_handle_bound_rename_is_exclusive_and_flushed_before_close() -> None:
    api = FakePublicationApi()

    rename_exclusive(SOURCE, DESTINATION, api)

    assert str(SOURCE) not in api.names
    assert api.names[str(DESTINATION)] == FileIdentity(7, 101)
    rename_index = next(index for index, call in enumerate(api.calls) if call[0] == "rename")
    flush_index = next(index for index, call in enumerate(api.calls) if call[0] == "flush")
    close_index = next(index for index, call in enumerate(api.calls) if call[0] == "close")
    assert rename_index < flush_index < close_index


def test_source_name_replacement_before_effect_fails_without_moving_foreign() -> None:
    api = FakePublicationApi()
    foreign = FileIdentity(7, 202)

    def replace_source() -> None:
        api.names[str(SOURCE)] = foreign

    with pytest.raises(WindowsPublicationError, match="source"):
        rename_exclusive(SOURCE, DESTINATION, api, before_effect=replace_source)

    assert api.names[str(SOURCE)] == foreign
    assert str(DESTINATION) not in api.names
    assert not any(call[0] == "rename" for call in api.calls)


def test_destination_race_fails_without_overwriting_foreign() -> None:
    api = FakePublicationApi()
    foreign = FileIdentity(7, 303)

    def occupy_destination() -> None:
        api.names[str(DESTINATION)] = foreign

    with pytest.raises(WindowsPublicationError, match="destination"):
        rename_exclusive(SOURCE, DESTINATION, api, before_effect=occupy_destination)

    assert api.names[str(SOURCE)] == FileIdentity(7, 101)
    assert api.names[str(DESTINATION)] == foreign
    assert not any(call[0] == "rename" for call in api.calls)


def test_native_failure_never_falls_back_to_path_rename() -> None:
    class FailingApi(FakePublicationApi):
        def rename_handle_exclusive(
            self,
            source: object,
            destination_parent: object,
            destination_name: str,
        ) -> None:
            raise OSError("unsupported")

    api = FailingApi()

    with pytest.raises(WindowsPublicationError, match="rename"):
        rename_exclusive(SOURCE, DESTINATION, api)

    assert api.names[str(SOURCE)] == FileIdentity(7, 101)
    assert str(DESTINATION) not in api.names
