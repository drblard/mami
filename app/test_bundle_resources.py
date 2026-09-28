import ast
from pathlib import Path
import unittest
from bundle_resources import WORKER_RESOURCES


class BundleResourceTests(unittest.TestCase):
    def test_local_worker_dependencies_are_packaged(self):
        root = Path(__file__).parent
        resources = set(WORKER_RESOURCES)
        self.assertEqual(len(resources), len(WORKER_RESOURCES))
        for name in resources:
            tree = ast.parse((root/name).read_text())
            for node in ast.walk(tree):
                modules = ([node.module] if isinstance(node, ast.ImportFrom) and node.module
                           else [alias.name for alias in node.names] if isinstance(node, ast.Import) else [])
                for module in modules:
                    dependency = module.split('.')[0] + '.py'
                    self.assertNotEqual(dependency, 'lab.py', f'{name} imports the experiment driver')
                    if (root/dependency).is_file():
                        self.assertIn(dependency, resources, f'{name} needs unpackaged {dependency}')


if __name__ == '__main__':
    unittest.main()
