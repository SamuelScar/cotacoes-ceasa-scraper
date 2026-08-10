import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from cotacoes_ceasa.storage.raw_html import RawHtmlStorage


class RawHtmlStorageDeduplicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.raw_dir = Path(self.temporary_directory.name)
        self.storage = RawHtmlStorage(self.raw_dir)

    def test_reuses_identical_text_without_creating_old_file(self) -> None:
        with patch("cotacoes_ceasa.storage.raw_html.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 10, 9, 0)
            first_path = self.storage.save("fonte", "categoria", "conteudo")
            clock.now.return_value = datetime(2026, 8, 11, 9, 0)
            repeated_path = self.storage.save("fonte", "categoria", "conteudo")

        self.assertEqual(first_path, repeated_path)
        self.assertEqual([first_path], list((self.raw_dir / "fonte").glob("*.html")))
        self.assertFalse((self.raw_dir / "fonte" / "old").exists())

    def test_reuses_identical_binary_content(self) -> None:
        with patch("cotacoes_ceasa.storage.raw_html.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 10, 9, 0)
            first_path = self.storage.save_bytes(
                "fonte",
                "categoria",
                b"pdf",
                "pdf",
            )
            clock.now.return_value = datetime(2026, 8, 11, 9, 0)
            repeated_path = self.storage.save_bytes(
                "fonte",
                "categoria",
                b"pdf",
                "pdf",
            )

        self.assertEqual(first_path, repeated_path)
        self.assertEqual(1, len(list((self.raw_dir / "fonte").glob("*.pdf"))))

    def test_changed_content_creates_new_raw_and_preserves_previous(self) -> None:
        with patch("cotacoes_ceasa.storage.raw_html.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 10, 9, 0)
            first_path = self.storage.save("fonte", "categoria", "versao 1")
            clock.now.return_value = datetime(2026, 8, 10, 10, 0)
            second_path = self.storage.save("fonte", "categoria", "versao 2")

        self.assertNotEqual(first_path, second_path)
        self.assertEqual("versao 2", second_path.read_text(encoding="utf-8"))
        archived_files = list((self.raw_dir / "fonte" / "old").glob("*.html"))
        self.assertEqual(1, len(archived_files))
        self.assertEqual("versao 1", archived_files[0].read_text(encoding="utf-8"))

    def test_category_prefix_does_not_reuse_another_scope(self) -> None:
        with patch("cotacoes_ceasa.storage.raw_html.datetime") as clock:
            clock.now.return_value = datetime(2026, 8, 10, 9, 0)
            short_path = self.storage.save("fonte", "fruta", "curto")
            clock.now.return_value = datetime(2026, 8, 10, 10, 0)
            self.storage.save("fonte", "fruta-extra", "longo")

        self.assertEqual(short_path, self.storage.find_latest("fonte", "fruta"))


if __name__ == "__main__":
    unittest.main()
