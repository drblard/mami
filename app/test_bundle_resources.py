import ast
from pathlib import Path
import unittest
from bundle_resources import RELEASE_TOOLS, WORKER_RESOURCES


class BundleResourceTests(unittest.TestCase):
    def test_local_dependencies_are_shipped(self):
        root = Path(__file__).parent
        for manifest, shipped in ((WORKER_RESOURCES, set(WORKER_RESOURCES)),
                                  (RELEASE_TOOLS, set(RELEASE_TOOLS) | set(WORKER_RESOURCES))):
            self.assertEqual(len(set(manifest)), len(manifest))
            self.assertTrue(all((root/name).exists() for name in manifest), 'manifest names a missing file')
            self.check_imports(root, [name for name in manifest if name.endswith('.py')], shipped)

    def check_imports(self, root, names, resources):
        for name in names:
            tree = ast.parse((root/name).read_text())
            for node in ast.walk(tree):
                modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module
                           else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    dependency = module.split('.')[0] + '.py'
                    if (root/dependency).is_file():
                        self.assertIn(dependency, resources, f'{name} needs unpackaged {dependency}')


if __name__ == '__main__':
    unittest.main()
