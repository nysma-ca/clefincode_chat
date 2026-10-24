"""Exercise the real legacy reader bodies without Frappe or media dependencies.

File permission decisions and all file/media I/O are fixtures. These tests check
that every reader honors the native decision before resolving or reading a path.
"""

import ast
import base64
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, mock_open, patch


API_ROOT = Path(__file__).resolve().parents[1] / "clefincode_chat" / "api"
VERSIONS = ("api_1_0_1", "api_1_2_1", "api_1_3_1", "api_1_3_3", "api_1_3_4")
READERS = (
    "get_file",
    "get_file_as_base64",
    "calculate_voice_clip_duration",
    "get_file_view_size",
    "ensure_image_thumbnail",
    "ensure_video_thumbnail",
)
CONTENT = b"synthetic media bytes"
ENCODED = base64.b64encode(CONTENT).decode("utf-8")


class PermissionDenied(Exception):
    pass


class MissingFile(Exception):
    pass


class FileFixture:
    def __init__(self, access):
        self.name = "fixture-file"
        self.is_private = access != "public"
        self.file_size = len(CONTENT)
        self.authorized = False
        self.allowed = access in ("public", "owned", "shared", "attachment")
        self.check_permission = Mock(side_effect=self._check_permission)
        self.get_full_path = Mock(side_effect=self._get_full_path)

    def _check_permission(self, permission):
        if permission != "read":
            raise AssertionError("Expected native File read permission")
        if not self.allowed:
            raise PermissionDenied
        self.authorized = True

    def _require_read(self):
        if not self.authorized:
            raise AssertionError("File path used before native read permission")

    @property
    def file_url(self):
        self._require_read()
        prefix = "/private/files/" if self.is_private else "/files/"
        return prefix + "fixture.bin"

    def _get_full_path(self):
        self._require_read()
        directory = "private" if self.is_private else "public"
        return f"/fake/site/{directory}/files/fixture.bin"


class ReaderFixture:
    def __init__(self, code, access="owned", missing=False, exists=True, major=16):
        self.file = FileFixture(access)
        self.message = SimpleNamespace(
            name="fixture-message", file_id=self.file.name, thumbnail_file=None,
            thumbnail_file_url=None, save=Mock(),
        )
        self.thumbnail = SimpleNamespace(name="fixture-thumbnail", file_url="/files/thumb.jpg")
        self.open = mock_open(read_data=CONTENT)
        self.exists = Mock(return_value=exists)
        self.save_file = Mock(return_value=self.thumbnail)
        self.save_thumb = Mock(return_value=self.thumbnail)
        self.image = Mock(mode="RGB")
        self.image.resize.return_value = self.image
        self.image.convert.return_value = self.image
        self.image.save.side_effect = lambda buffer, **kwargs: buffer.write(CONTENT)
        self.images = SimpleNamespace(
            open=Mock(return_value=self.image), fromarray=Mock(return_value=self.image),
            ANTIALIAS=1, LANCZOS=1, Resampling=SimpleNamespace(LANCZOS=1),
        )
        self.video = MagicMock(duration=65.25)
        self.video.__enter__.return_value = self.video
        self.video_factory = Mock(return_value=self.video)
        self.audio = SimpleNamespace(from_file=Mock(return_value=SimpleNamespace(duration_seconds=65.25)))
        self.process = SimpleNamespace(
            run=Mock(return_value=SimpleNamespace(stdout='{"format": {"duration": "65.25"}}')),
            DEVNULL=-3,
        )
        site_path = Mock(side_effect=lambda *parts: os.path.join("/fake/site", *parts))
        self.frappe = SimpleNamespace(
            __version__=f"{major}.53.0", PermissionError=PermissionDenied,
            DoesNotExistError=MissingFile,
            get_doc=Mock(side_effect=MissingFile) if missing else Mock(return_value=self.file),
            get_site_path=site_path, utils=SimpleNamespace(get_site_path=site_path),
            log_error=Mock(), get_traceback=Mock(return_value="fixture traceback"),
            throw=Mock(side_effect=ValueError),
        )
        self.namespace = dict(
            frappe=self.frappe, open=self.open, base64=base64, io=io, BytesIO=io.BytesIO,
            os=SimpleNamespace(path=SimpleNamespace(
                exists=self.exists, join=os.path.join, abspath=os.path.abspath,
            )),
            json=json, subprocess=self.process, Image=self.images,
            VideoFileClip=self.video_factory, save_file=self.save_file,
            _save_thumb_file=self.save_thumb,
        )
        exec(code, self.namespace)

    def call(self, reader, **kwargs):
        if reader.startswith("ensure_"):
            args = (self.message,)
        else:
            args = (self.file.name,)
        with patch.dict("sys.modules", {"pydub": SimpleNamespace(AudioSegment=self.audio)}):
            return self.namespace[reader](*args, **kwargs)

    def assert_no_io(self):
        for operation in (
            self.file.get_full_path, self.frappe.get_site_path, self.exists,
            self.open, self.images.open, self.images.fromarray, self.video_factory,
            self.audio.from_file, self.process.run, self.save_file, self.save_thumb,
            self.message.save,
        ):
            operation.assert_not_called()


class FileReaderPermissionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code = {}
        cls.readers = {}
        for version in VERSIONS:
            path = API_ROOT / version / "api.py"
            tree = ast.parse(path.read_text(), filename=str(path))
            selected = []
            cls.readers[version] = []
            for node in tree.body:
                if isinstance(node, ast.FunctionDef) and node.name in (*READERS, "_get_file_doc_by_id"):
                    node.decorator_list = []
                    selected.append(node)
                    if node.name in READERS:
                        cls.readers[version].append(node.name)
            cls.code[version] = compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec")

    def cases(self):
        for version in VERSIONS:
            for reader in self.readers[version]:
                yield version, reader

    def test_reader_inventory(self):
        self.assertEqual(sum(len(readers) for readers in self.readers.values()), 23)
        self.assertEqual(sum("get_file" in readers for readers in self.readers.values()), 5)
        self.assertEqual(sum("get_file_as_base64" in readers for readers in self.readers.values()), 4)

    def test_denied_private_file_stops_before_any_io(self):
        for version, reader in self.cases():
            for kwargs in ({}, {"is_video": True}) if reader == "get_file_view_size" else ({},):
                with self.subTest(version=version, reader=reader, kwargs=kwargs):
                    fixture = ReaderFixture(self.code[version], access="unrelated-private")
                    with self.assertRaises(PermissionDenied):
                        fixture.call(reader, **kwargs)
                    fixture.file.check_permission.assert_called_once_with("read")
                    fixture.assert_no_io()
                    fixture.frappe.log_error.assert_not_called()

    def test_native_allowed_files_keep_results(self):
        for version, reader in self.cases():
            for access in ("public", "owned", "shared", "attachment"):
                with self.subTest(version=version, reader=reader, access=access):
                    fixture = ReaderFixture(self.code[version], access=access)
                    result = fixture.call(reader)
                    fixture.file.check_permission.assert_called_once_with("read")
                    if reader == "get_file":
                        self.assertEqual(result, {"results": ENCODED})
                    elif reader == "get_file_as_base64":
                        self.assertEqual(result, {"results": {"compressed_base64": ENCODED}})
                    elif reader == "calculate_voice_clip_duration":
                        self.assertEqual(result, {"results": [{"duration": "01:05"}]})
                    elif reader == "get_file_view_size":
                        self.assertEqual(result, {"results": [{
                            "file_size": len(CONTENT), "data": ENCODED, "duration": None,
                        }]})
                    else:
                        self.assertEqual(result, fixture.thumbnail.name)
                        self.assertEqual(fixture.message.thumbnail_file, fixture.thumbnail.name)
                        operation = fixture.save_file if reader == "ensure_image_thumbnail" else fixture.save_thumb
                        if reader == "ensure_image_thumbnail":
                            self.assertEqual(operation.call_args.kwargs["is_private"], fixture.file.is_private)
                        else:
                            self.assertEqual(operation.call_args.args[-1], fixture.file.is_private)

    def test_video_dimensions_keep_result(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                fixture = ReaderFixture(self.code[version])
                self.assertEqual(fixture.call("get_file_view_size", is_video=True), {"results": [{
                    "file_size": len(CONTENT), "data": ENCODED, "duration": 65.25,
                }]})
                fixture.file.check_permission.assert_called_once_with("read")

    def test_unformatted_duration_and_legacy_fallback(self):
        for version in VERSIONS:
            for major in (15, 16):
                with self.subTest(version=version, major=major):
                    fixture = ReaderFixture(self.code[version], major=major)
                    self.assertEqual(fixture.call("calculate_voice_clip_duration", formatted=False), {
                        "results": [{"duration": 65.25}],
                    })
                    fixture.file.check_permission.assert_called_once_with("read")
                    if version == "api_1_2_1" and major == 16:
                        fixture.process.run.assert_called_once()
                        fixture.audio.from_file.assert_not_called()
                    else:
                        fixture.audio.from_file.assert_called_once()
                        fixture.process.run.assert_not_called()

    def test_missing_file_keeps_existing_error_contract(self):
        for version, reader in self.cases():
            with self.subTest(version=version, reader=reader):
                fixture = ReaderFixture(self.code[version], missing=True)
                if reader in ("get_file_as_base64", "ensure_video_thumbnail"):
                    self.assertIsNone(fixture.call(reader))
                else:
                    with self.assertRaises(MissingFile):
                        fixture.call(reader)
                fixture.file.check_permission.assert_not_called()
                fixture.assert_no_io()

    def test_missing_audio_on_disk_keeps_existing_result(self):
        for version in VERSIONS:
            with self.subTest(version=version):
                fixture = ReaderFixture(self.code[version], exists=False)
                expected = {"results": [{"duration": 0}]} if version == "api_1_2_1" else {
                    "error": "File does not exist at the specified path",
                }
                self.assertEqual(fixture.call("calculate_voice_clip_duration"), expected)
                fixture.file.check_permission.assert_called_once_with("read")
                fixture.audio.from_file.assert_not_called()
                fixture.process.run.assert_not_called()

    def test_cached_thumbnail_does_not_read_or_process_file(self):
        for version in ("api_1_3_3", "api_1_3_4"):
            for reader in ("ensure_image_thumbnail", "ensure_video_thumbnail"):
                with self.subTest(version=version, reader=reader):
                    fixture = ReaderFixture(self.code[version])
                    fixture.message.thumbnail_file = "cached-thumbnail"
                    self.assertEqual(fixture.call(reader), "cached-thumbnail")
                    fixture.frappe.get_doc.assert_not_called()
                    fixture.assert_no_io()


if __name__ == "__main__":
    unittest.main()
