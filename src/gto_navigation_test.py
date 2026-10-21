"""Execute the student safe-return JavaScript contract with CI's Node runtime.

The production Python image does not ship Node; its complete HTTP/XLSX report
matrix lives separately in gto_report_test.py and runs in exact-image CI.
"""
import json
import shutil
import subprocess
from pathlib import Path


def test_client_safe_return_matches_exact_report_route_without_broadening():
    node = shutil.which('node')
    assert node, 'Node is required for the actual student navigation regression'
    script = Path('src/static/scripts/script.js').read_text()
    start = script.index('class StudentProfilePage {')
    end = script.index('// Keep the native select', start)
    source = script[start:end] + '\nglobalThis.StudentProfilePage = StudentProfilePage;'
    harness = '''
const vm = require('vm');
const source = JSON.parse(process.argv[1]);
const context = {URL, location: {origin: 'https://synthetic.example'}};
vm.createContext(context); vm.runInContext(source, context);
const page = Object.create(context.StudentProfilePage.prototype);
const values = ['/reports/gto?year=2026&q=000123', '/reports', '/reports/gto/export.xlsx',
    '/reports/gto/extra', '//bad.invalid', '/reports/gto?x=%0a'];
process.stdout.write(JSON.stringify(values.map(value => page.safeReturn(value))));
'''
    result = subprocess.run([node, '-e', harness, json.dumps(source)], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == ['/reports/gto?year=2026&q=000123', '/reports', None, None, None, None]
