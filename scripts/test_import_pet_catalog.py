import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("import_pet_catalog.py")


class ImportCatalogTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.spec_from_file_location("catalog_import", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def fixture(self, directory):
        source = directory / "source"
        source.mkdir()
        image = source / "image.png"
        image.write_bytes(b"test-public-image")
        row = {"pet_id": "1000101", "name": None, "image_path": "image.png", "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(), "asset_path": "Assets/Avatar_1000101.png"}
        catalog = source / "pet-catalog.json"
        catalog.write_text(json.dumps({"catalog_sha256": "test-catalog", "pets": [row]}), encoding="utf-8")
        return catalog, row

    def test_copies_verified_image_and_keeps_names_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            catalog, row = self.fixture(directory)
            target = directory / "output"
            result = self.module().import_catalog(catalog, target)
            self.assertTrue((target / "catalog.json").is_file(), "Import must produce the public catalog")
            saved = json.loads((target / "catalog.json").read_text("utf-8"))
            self.assertEqual(saved["pets"][0]["image_path"], "images/1000101.png")
            self.assertFalse(saved["pets"][0]["name_verified"])
            self.assertIsNone(saved["pets"][0]["name"])
            self.assertEqual((target / "images/1000101.png").read_bytes(), b"test-public-image")
            self.assertEqual(result["image_count"], 1)

    def test_rejects_corrupted_image_without_creating_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            catalog, row = self.fixture(directory)
            (catalog.parent / "image.png").write_bytes(b"corrupted")
            target = directory / "output"
            with self.assertRaises(ValueError):
                self.module().import_catalog(catalog, target)
            self.assertFalse((target / "catalog.json").exists())

    def test_rejects_image_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            catalog, row = self.fixture(directory)
            row["image_path"] = "../outside.png"
            catalog.write_text(json.dumps({"pets": [row]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                self.module().import_catalog(catalog, directory / "output")

    def test_keeps_existing_verified_name_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            catalog, row = self.fixture(directory)
            target = directory / "output"
            target.mkdir()
            original = '{"names":{"1000101":{"name":"verified-name","source":"test-evidence"}}}'
            (target / "names.json").write_text(original, encoding="utf-8")
            self.module().import_catalog(catalog, target)
            self.assertTrue((target / "catalog.json").is_file(), "Import must still update the public catalog")
            self.assertEqual((target / "names.json").read_text("utf-8"), original)


if __name__ == "__main__":
    unittest.main()
